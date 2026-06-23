import asyncio
import copy
import json
import os
import tempfile
import time
from pathlib import Path
from pprint import pprint

import requests
import socket
import subprocess
from dotenv import load_dotenv

load_dotenv()


class VPNMonitor:
    """
        Happ-подобные HTTP-заголовки, фильтрует подходящие VLESS-конфигурации,
    нормализует блоки `outbounds` под формат Xray и тестирует каждую ноду
    через временно запущенный локальный Xray-процесс.

    Для проверки скорости создаётся временный Xray config с локальным SOCKS5
    inbound, после чего выполняется HTTP-запрос к `https://api.telegram.org`
    через `curl`. По результатам измеряются параметры соединения:
    время подключения, время до первого байта, общее время запроса и скорость
    загрузки ответа.

    Основной сценарий работы:
    - получить список VPN-серверов через `get_vpn_servers`;
    - создать временный Xray config через `build_temp_config`;
    - запустить Xray для каждой конфигурации через `test_candidate`;
    - выполнить проверочный запрос через `curl_test`;
    - выбрать лучший `outbounds` по минимальному `time_total`.

    Атрибуты:
        xray_url: URL для получения списка VPN-конфигураций.
        x_hwid: аппаратный идентификатор, передаваемый в заголовке X-Hwid.
        url: адрес, по которому проверяется доступность Telegram API.
        timeout: максимальное время ожидания curl-запроса в секундах.
        vpn_servers: список VLESS-серверов, полученных из удалённого источника.

    Возвращаемое значение основного сценария:
        Метод `get_best_node` возвращает блок `outbounds` лучшей найденной ноды
        либо `None`, если список серверов пустой или ни одна нода не прошла тест.
    """

    def __init__(self):
        self.xray_url = os.getenv("xray_url")
        self.x_hwid = os.getenv("X-Hwid")
        self.port_client_xray = os.getenv("port_client_xray")

        self.url = "https://api.telegram.org"
        self.timeout = 12
        self.vpn_servers = self.get_vpn_servers()

    def get_vpn_servers(self) -> list:
        headers = {
            "User-Agent": "Happ/2.8.0/Windows/2604081205607",
            "X-App-Version": "2.8.0",
            "X-Device-Locale": "RU",
            "X-Device-Os": "Windows",
            "X-Device-Model": "AO-x86_64",
            "X-Hwid": self.x_hwid,
            "X-Ver-Os": "10_10.0.19045",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "ru-RU,en,*",
        }
        try:
            response = requests.get(self.xray_url, headers=headers)
        except:
            print('Ошибка получения списка нод с https://sub.harknmav.fun')
            return []

        if response.status_code != 200:
            print(f'{response.status_code=}')
            return []
        response.encoding = "utf-8"
        txt = response.text.strip()
        ls_vpn = json.loads(txt)
        ls_vpn = [vpn for vpn in ls_vpn if vpn['outbounds'][0]['protocol'] == 'vless']
        for n, node in enumerate(ls_vpn):
            node['num_node'] = n
        return ls_vpn

    def _get_free_port_sync(self) -> int:
        """Синхронно возвращает свободный локальный TCP-порт."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def get_free_port(self) -> int:
        """
        Асинхронно возвращает свободный локальный TCP-порт.

        Сама операция с socket является синхронной, поэтому она выполняется
        в отдельном потоке через asyncio.to_thread(), чтобы не блокировать event loop.
        """
        return await asyncio.to_thread(self._get_free_port_sync)

    def normalize_outbounds(self, outbounds: list) -> list:
        """
        Нормализует outbounds из Happ под формат Xray.

        Некоторые клиенты могут отдавать поля в формате,
        который Xray не принимает напрямую.
        """
        outbounds = copy.deepcopy(outbounds)

        for outbound in outbounds:
            stream_settings = outbound.get("streamSettings", {})
            grpc_settings = stream_settings.get("grpcSettings", {})

            # В Xray grpcSettings.mode обычно строка или поле отсутствует.
            # Если Happ отдал False, лучше убрать поле.
            if grpc_settings.get("mode") is False:
                grpc_settings.pop("mode", None)

        return outbounds

    def build_temp_config(self, outbounds: list, port: int) -> dict:
        """Создаёт временный Xray config для теста набора outbounds."""
        outbounds = self.normalize_outbounds(outbounds)

        return {
            "log": {
                "loglevel": "debug"
            },
            "inbounds": [
                {
                    "tag": "test-socks",
                    "listen": "127.0.0.1",
                    "port": port,
                    "protocol": "socks",
                    "settings": {
                        "auth": "noauth",
                        "udp": False
                    }
                }
            ],
            "outbounds": outbounds,
            "routing": {
                "rules": [
                    {
                        "type": "field",
                        "inboundTag": ["test-socks"],
                        "outboundTag": "proxy"
                    }
                ]
            }
        }

    async def curl_test(self, port) -> dict:
        """
        Асинхронно выполняет HTTP-запрос через локальный SOCKS5 Xray.

        Возвращает:
        - ok: True, если HTTP-код не равен "000", то есть запрос дошёл до сервера.
        - http_code: HTTP-статус ответа, например "200", "403", "502" или "000" при ошибке соединения.
        - time_connect: время в секундах до установки TCP-соединения с целевым сервером.
        - time_starttransfer: время в секундах до получения первого байта ответа от сервера.
        - time_total: полное время выполнения запроса в секундах.
        - speed_download: средняя скорость скачивания ответа в байтах в секунду.
        - elapsed: фактическое время выполнения subprocess-запуска curl в Python.
        """
        cmd = [
            "curl",
            "--proxy",
            f"socks5h://127.0.0.1:{port}",
            "-L",
            "-o",
            os.devnull,
            "-sS",
            "--connect-timeout",
            str(self.timeout),
            "--max-time",
            str(self.timeout),
            "-w",
            "%{http_code} %{time_connect} %{time_starttransfer} %{time_total} %{speed_download}",
            self.url,
        ]

        started = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout_bytes, stderr_bytes = await proc.communicate()
        elapsed = time.monotonic() - started

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return {
                "ok": False,
                "error": stderr or stdout,
                "elapsed": elapsed
            }

        parts = stdout.split()
        if len(parts) != 5:
            return {
                "ok": False,
                "error": f"Неожиданный вывод curl: {stdout!r}",
                "elapsed": elapsed
            }

        http_code, time_connect, time_starttransfer, time_total, speed_download = parts

        return {
            "ok": http_code != "000",
            "http_code": http_code,
            "time_connect": float(time_connect),
            "time_starttransfer": float(time_starttransfer),
            "time_total": float(time_total),
            "speed_download": float(speed_download),
            "elapsed": elapsed,
        }

    def diagnoctic(self):
        project_dir = Path(__file__).resolve().parent
        xray_bin = project_dir / "xray" / "xray.exe"

        outbound = self.vpn_servers[0]['outbound']

        port = self.get_free_port()
        temp_config = self.build_temp_config(outbound, port)

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as file:
            json.dump(temp_config, file, ensure_ascii=False, indent=2)
            temp_config_path = file.name

        proc = subprocess.Popen(
            [str(xray_bin), "run", "-config", temp_config_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            time.sleep(1.2)

            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                return {
                    "ok": False,
                    "error": f"Xray не стартовал: {stderr.strip()}"
                }

            # result = await self.curl_test(port)
            # result['outbound'] = outbound
            return ''

        finally:
            proc.terminate()

            try:
                proc.wait()
            except subprocess.TimeoutExpired:
                proc.kill()

            try:
                os.remove(temp_config_path)
            except OSError:
                pass

    async def test_candidate(self, xray_bin: str, num_node) -> dict:
        """Тестирует один JSON-кандидат через временный Xray-процесс."""
        port = await self.get_free_port()
        outbound = self.vpn_servers[num_node]['outbounds']
        temp_config = self.build_temp_config(outbound, port)

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as file:
            json.dump(temp_config, file, ensure_ascii=False, indent=2)
            temp_config_path = file.name

        proc = subprocess.Popen(
            [str(xray_bin), "run", "-config", temp_config_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            await asyncio.sleep(1.2)

            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                return {
                    "ok": False,
                    "error": f"Xray не стартовал: {stderr.strip()}"
                }

            result = await self.curl_test(port)
            result['num_node'] = num_node
            return result

        finally:
            proc.terminate()

            try:
                await asyncio.to_thread(proc.wait, timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

            try:
                os.remove(temp_config_path)
            except OSError:
                pass

    async def get_best_node(self):
        if not len(self.vpn_servers):
            return

        project_dir = Path(__file__).resolve().parent
        xray_bin = project_dir / "xray" / "xray.exe"

        tasks = [asyncio.create_task(self.test_candidate(xray_bin, row["num_node"])) for row in self.vpn_servers]
        best_time_total = 100
        num_node = None

        for coro in asyncio.as_completed(tasks):
            result = await coro
            # print(result)
            if result['ok'] and result['time_total'] < best_time_total:
                best_time_total = result['time_total']
                num_node = result['num_node']

        # print('*********************')
        # print(f'{best_time_total=}')
        # print(f'{num_node=}')

        best_node = self.vpn_servers[num_node]
        return best_node

    def create_new_config(self, best_node: dict) -> dict:
        best_node = copy.deepcopy(best_node)
        best_node.pop("num_node", None)

        best_node["inbounds"] = [
            {
                "listen": "127.0.0.1",
                "port": self.port_client_xray,
                "sniffing": {
                    "routeOnly": False,
                    "enabled": True,
                    "destOverride": [
                        "http",
                        "quic",
                        "tls"
                    ]
                },
                "protocol": "socks",
                "settings": {
                    "udp": True
                },
                "tag": "socks"
            }
        ]
        best_node["outbounds"] = self.normalize_outbounds(best_node["outbounds"])
        return best_node

    def save_new_config(self, new_config: dict):
        project_dir = Path(__file__).resolve().parent
        config_path = project_dir / "new_config.json"

        with config_path.open("w", encoding="utf-8") as file:
            json.dump(new_config, file, ensure_ascii=False, indent=2)

    async def run_pipeline(self):
        print("Выбор лучшей ноды...")
        best_node = await self.get_best_node()

        # pprint(best_node)

        if not best_node:
            print("Не удалось выбрать лучшую ноду.")
            return

        print(f"Лучшая нода: {best_node['remarks']}")
        new_config = self.create_new_config(best_node)
        self.save_new_config(new_config)
        print("Файл конфигурации создан.")

    async def scheduler(self, interval: int = 30):
        """ Асинхронный scheduler.

        Запускает `curl_test()` в бесконечном цикле с паузой `interval` секунд
        между завершением одного запуска и началом следующего.

        Args:
            interval: пауза между запусками в секундах.
        """
        while True:
            try:
                res = await self.curl_test(self.port_client_xray)
                if not res['ok']:
                    print(f"{res}\nСтарт обновления конфигурации xray.")
                    await self.run_pipeline()

            except Exception as error:
                print(f"Ошибка в scheduler: {error}")

            await asyncio.sleep(interval)


if __name__ == '__main__':
    vpn = VPNMonitor()
    # print(vpn.vpn_servers)
    # asyncio.run(vpn.get_best_node())
    asyncio.run(vpn.run_pipeline())
    # asyncio.run(vpn.scheduler())
    # vpn.diagnoctic()
