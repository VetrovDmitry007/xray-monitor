import asyncio
import csv
import io
import os
import signal
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

from log_utils import get_logger

load_dotenv()


@dataclass
class PortOwner:
    """Информация о процессе, который занимает локальный порт."""
    pid: int
    process_name: Optional[str]
    local_address: str


class TelegramProxyClient:
    """
    Запускает локальный Xray SOCKS5-клиент для подключения aiogram к Telegram.

    Использует уже готовый файл `new_config.json`.
    После запуска выставляет:
        TELEGRAM_PROXY_URL=socks5://127.0.0.1:1080
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        xray_bin: Optional[Path] = None,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        startup_timeout: float = 10.0,
        allow_existing_xray: bool = True,
    ):
        self.logger = get_logger(__name__)

        self.project_dir = Path(__file__).resolve().parent

        self.config_path = config_path or self.project_dir / "new_config.json"
        self.xray_bin = xray_bin or self._get_default_xray_bin()

        self.host = host
        self.port = port or int(os.getenv("port_client_xray", "1080"))
        self.startup_timeout = startup_timeout
        self.allow_existing_xray = allow_existing_xray

        self.proxy_url = os.getenv(
            "TELEGRAM_PROXY_URL",
            f"socks5://{self.host}:{self.port}",
        )

        self.process: Optional[subprocess.Popen] = None
        self.log_path = self.project_dir / "logs" / "xray_proxy.log"
        self._log_file = None

    def _get_default_xray_bin(self) -> Path:
        """Возвращает путь к бинарнику Xray."""
        if os.name == "nt":
            return self.project_dir / "xray" / "xray.exe"

        return self.project_dir / "xray" / "xray"

    def _is_port_open_sync(self) -> bool:
        """Проверяет, открыт ли локальный TCP-порт."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((self.host, self.port)) == 0

    async def is_port_open(self) -> bool:
        """Асинхронная обёртка над проверкой локального порта."""
        return await asyncio.to_thread(self._is_port_open_sync)

    def _get_windows_process_name(self, pid: int) -> Optional[str]:
        """Возвращает имя процесса Windows по PID через tasklist."""
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="cp866",
            errors="replace",
        )

        output = result.stdout.strip()

        if not output or output.startswith("INFO:"):
            return None

        rows = list(csv.reader(io.StringIO(output)))

        if not rows or not rows[0]:
            return None

        return rows[0][0]

    def _get_port_owner_sync(self) -> Optional[PortOwner]:
        """
        Возвращает процесс, который слушает нужный порт.

        На Windows использует:
            netstat -ano -p tcp
            tasklist /FI "PID eq ..."
        """
        if os.name != "nt":
            return None

        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="cp866",
            errors="replace",
        )

        for line in result.stdout.splitlines():
            parts = line.split()

            if len(parts) < 5:
                continue

            protocol = parts[0].upper()
            local_address = parts[1]
            state = parts[3].upper()
            pid_raw = parts[4]

            if protocol != "TCP":
                continue

            if state != "LISTENING":
                continue

            if not pid_raw.isdigit():
                continue

            # Важно: проверяем именно порт 1080, а не 10808/10809.
            local_port = local_address.rsplit(":", 1)[-1]

            if local_port != str(self.port):
                continue

            pid = int(pid_raw)
            process_name = self._get_windows_process_name(pid)

            return PortOwner(
                pid=pid,
                process_name=process_name,
                local_address=local_address,
            )

        return None

    async def get_port_owner(self) -> Optional[PortOwner]:
        """Асинхронно возвращает владельца локального порта."""
        return await asyncio.to_thread(self._get_port_owner_sync)

    async def _check_port_before_start(self) -> bool:
        """
        Проверяет порт перед запуском Xray.

        Returns:
            True, если порт уже занят существующим xray.exe и его можно использовать.
            False, если порт свободен и можно запускать новый Xray.

        Raises:
            RuntimeError, если порт занят не Xray-процессом.
        """
        owner = await self.get_port_owner()

        if owner:
            process_name = owner.process_name or "unknown"
            xray_name = self.xray_bin.name.lower()

            if self.allow_existing_xray and process_name.lower() == xray_name:
                self.logger.info(
                    f"Порт {self.host}:{self.port} уже занят Xray: "
                    f"{process_name}, PID={owner.pid}. Используем существующий прокси."
                )
                return True

            raise RuntimeError(
                f"Порт {self.host}:{self.port} уже занят процессом "
                f"{process_name}, PID={owner.pid}. "
                f"Освободи порт командой: taskkill /PID {owner.pid} /F"
            )

        if await self.is_port_open():
            raise RuntimeError(
                f"Порт {self.host}:{self.port} занят, "
                f"но определить владельца через netstat/tasklist не удалось."
            )

        self.logger.info(f"Порт {self.host}:{self.port} свободен.")
        return False

    def _validate_files(self) -> None:
        """Проверяет наличие `new_config.json` и бинарника Xray."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Не найден файл конфигурации Xray: {self.config_path}. "
                f"Сначала нужно выполнить VPNMonitor.run_pipeline(), "
                f"чтобы создать new_config.json."
            )

        if not self.xray_bin.exists():
            raise FileNotFoundError(
                f"Не найден бинарник Xray: {self.xray_bin}"
            )

    async def wait_until_ready(self) -> None:
        """Ожидает, пока локальный SOCKS5-порт станет доступен."""
        started_at = asyncio.get_running_loop().time()

        while True:
            if await self.is_port_open():
                return

            if self.process and self.process.poll() is not None:
                raise RuntimeError(
                    f"Xray завершился сразу после запуска. "
                    f"Проверь лог: {self.log_path}"
                )

            elapsed = asyncio.get_running_loop().time() - started_at

            if elapsed > self.startup_timeout:
                raise TimeoutError(
                    f"Xray не поднял SOCKS5 на {self.host}:{self.port} "
                    f"за {self.startup_timeout} секунд. "
                    f"Проверь new_config.json и лог: {self.log_path}"
                )

            await asyncio.sleep(0.2)

    async def start(self) -> str:
        """
        Запускает Xray с `new_config.json`.

        Возвращает proxy URL для aiogram.
        """
        os.environ["TELEGRAM_PROXY_URL"] = self.proxy_url

        use_existing_xray = await self._check_port_before_start()

        if use_existing_xray:
            return self.proxy_url

        self._validate_files()

        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self.logger.info(
            f"Запуск Xray: {self.xray_bin} run -config {self.config_path}"
        )

        self._log_file = self.log_path.open("a", encoding="utf-8")

        creationflags = 0

        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            self.process = subprocess.Popen(
                [
                    str(self.xray_bin),
                    "run",
                    "-config",
                    str(self.config_path),
                ],
                stdout=self._log_file,
                stderr=self._log_file,
                text=True,
                creationflags=creationflags,
            )

            await self.wait_until_ready()

        except Exception:
            await self.stop()
            raise

        self.logger.info(f"Xray SOCKS5 поднят: {self.proxy_url}")

        return self.proxy_url

    async def stop(self) -> None:
        """Останавливает Xray-процесс, если он был запущен этим модулем."""
        if self.process and self.process.poll() is None:
            self.logger.info("Остановка Xray...")

            self.process.terminate()

            try:
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                self.logger.warning("Xray не завершился мягко, выполняю kill.")
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)

        if self._log_file and not self._log_file.closed:
            self._log_file.close()

    async def __aenter__(self) -> "TelegramProxyClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()


async def main():
    proxy_client = TelegramProxyClient()

    try:
        proxy_url = await proxy_client.start()
    except Exception as error:
        print(f"Ошибка запуска Xray-прокси: {error}")
        return

    print("Xray запущен.")
    print(f"TELEGRAM_PROXY_URL={proxy_url}")
    print("Для остановки нажми Ctrl+C.")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        if not stop_event.is_set():
            print("\nПолучен сигнал остановки. Останавливаю Xray...")
            stop_event.set()

    if os.name == "nt":
        signal.signal(
            signal.SIGINT,
            lambda signum, frame: loop.call_soon_threadsafe(request_stop),
        )

        if hasattr(signal, "SIGBREAK"):
            signal.signal(
                signal.SIGBREAK,
                lambda signum, frame: loop.call_soon_threadsafe(request_stop),
            )
    else:
        loop.add_signal_handler(signal.SIGINT, request_stop)
        loop.add_signal_handler(signal.SIGTERM, request_stop)

    try:
        await stop_event.wait()
    finally:
        await proxy_client.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")