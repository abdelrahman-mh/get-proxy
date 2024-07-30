import re
import sys
import asyncio
import os
import time
from aiohttp import ClientSession, ClientTimeout, ClientError
from datetime import datetime, timedelta


class ProxyConfig:
    def __init__(
        self,
        prefix="http://",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36",
        ip_check_api="http://httpbin.org/ip",
        request_timeout=15,
        retry=0,
        concurrency_limit=500,
        proxy_sources_file="proxy_sources.txt",
        proxy_cache_file="proxy_cache.txt",
        cache_enabled=False,
        cache_duration_minutes=20,
        enforce_unique_ip=True,
        strict_x_forwarded_for=False,
    ):
        self.prefix = prefix
        self.user_agent = user_agent
        self.ip_check_api = ip_check_api
        self.request_timeout = request_timeout
        self.retry = retry
        self.concurrency_limit = concurrency_limit
        self.proxy_sources_file = proxy_sources_file
        self.proxy_cache_file = proxy_cache_file
        self.cache_enabled = cache_enabled
        self.cache_duration = timedelta(minutes=cache_duration_minutes)
        self.enforce_unique_ip = enforce_unique_ip
        self.strict_x_forwarded_for = strict_x_forwarded_for


class ProxyFetcher:
    PROXY_REGEX = re.compile(
        r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:\s+|\s*:\s*)\d{2,5})"
    )
    IP_V4_REGEX = re.compile(
        r"\b((?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))\b"
    )
    JSON_CONFIG_REGEX = re.compile(r".*\sjson=true&ip=([^&]+)&port=([^&]+)$")

    def __init__(self, config: ProxyConfig = ProxyConfig()):
        self.config = config
        self._IPs = set()
        self._session = ClientSession()
        self._semaphore = asyncio.Semaphore(self.config.concurrency_limit)
        self._start_time = time.time()
        self._monitor_status_task = None
        self._status = {
            "total_proxy": 0,
            "valid_proxy": 0,
            "invalid_proxy": 0,
            "total_sources": 0,
            "valid_sources": 0,
            "invalid_sources": 0,
            "pending_sources": 0,
        }

    async def __print_monitoring_info(self, infinite: bool = True):
        reset = "\033[0m"
        green = "\033[32m"
        blue = "\033[34m"
        red = "\033[31m"
        yellow = "\033[33m"
        cyan = "\033[36m"
        while True:
            p_len = len(str(self._status["total_proxy"]))
            s_len = len(str(self._status["total_sources"]))
            sys.stdout.write(
                f"\r{blue}[+] Validating {green}{self._status['valid_proxy']+self._status['invalid_proxy']:{p_len}}/{self._status['total_proxy']} "
                f"({green}✔ {self._status['valid_proxy']:<{p_len}} {red}✗ {self._status['invalid_proxy']:<{p_len}}{green}){blue}    "
                f"URLs {green}{self._status['valid_sources']+self._status['invalid_sources']:{s_len}}/{self._status['total_sources']} "
                f"({yellow}pending {self._status['pending_sources']:<{s_len}} "
                f"{green}✔ {self._status['valid_sources']:<{s_len}} {red}✗ {self._status['invalid_sources']:<{s_len}}{green})     "
                f"{cyan}{time.time() - self._start_time:.2f}s{reset}"
            )

            # sys.stdout.flush()
            # [+] Validating  10/413 (✔ 7   ✗ 6)    URLs  2/11 (pending 5  ✔ 10 ✗ 1)   45s
            if not infinite:
                break
            await asyncio.sleep(1)

    async def __aenter__(self):
        if self.config.enforce_unique_ip:
            self._IPs.add(await self.__get_public_ip())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._session.close()
        if self._monitor_status_task:
            self._monitor_status_task.cancel()
        return True

    async def __get_public_ip(self) -> str:
        print("\rGetting public IP...")
        try:
            async with self._session.get(self.config.ip_check_api) as response:
                response.raise_for_status()
                text = await response.text()
                match = self.IP_V4_REGEX.search(text)
                if match:
                    print(f"\rPublic IP: \033[33m{match.group()}")
                    return match.group()
                else:
                    raise ValueError("Response did not contain a valid IP address")
        except ClientError as e:
            raise RuntimeError(f"Failed to fetch public IP address: {e}")

    def __extract_proxies_from_json(
        self, json_text: str, ip_field: str, port_field: str
    ):
        ip_regex = f'"{ip_field}"\s*:\s*"(.*?)"'
        port_regex = f'"{port_field}"\s*:\s*"(.*?)"'
        ip_matches = re.findall(ip_regex, json_text)
        port_matches = re.findall(port_regex, json_text)
        return [f"{ip}:{port}" for ip, port in zip(ip_matches, port_matches)]

    async def __validate_proxy(self, proxy: str) -> str:
        for _ in range(self.config.retry + 1):
            try:
                async with self._semaphore:
                    async with self._session.get(
                        self.config.ip_check_api,
                        proxy=proxy,
                        timeout=ClientTimeout(self.config.request_timeout),
                    ) as response:
                        response.raise_for_status()
                        if not self.config.enforce_unique_ip:
                            self._status["valid_proxy"] += 1
                            return proxy

                        text = await response.text()

                        # ignore X-Forwarded-For header
                        if self.config.strict_x_forwarded_for:
                            match = self.IP_V4_REGEX.findall(text)
                            ip = ",".join(match.group()) if match else None
                        else:
                            match = self.IP_V4_REGEX.search(text)
                            ip = match.group() if match else None

                        if ip and ip not in self._IPs:
                            self._IPs.add(ip)
                            self._status["valid_proxy"] += 1
                            return proxy
                        else:
                            self._status["invalid_proxy"] += 1
                            return None
            except Exception:
                pass
        self._status["invalid_proxy"] += 1
        return None

    async def __fetch_proxies_from_source(self, url: str):
        self._status["pending_sources"] += 1
        try:
            url_parts = url.split(" ")
            base_url = url_parts[0]
            json_config = url_parts[1] if len(url_parts) > 1 else None

            async with self._session.get(
                base_url, headers={"User-Agent": self.config.user_agent}
            ) as response:
                text = await response.text()

                if json_config and self.JSON_CONFIG_REGEX.match(url):
                    # Extract IP and port field names from JSON config
                    match = self.JSON_CONFIG_REGEX.match(url)
                    ip_field = match.group(1)
                    port_field = match.group(2)
                    proxies = self.__extract_proxies_from_json(
                        text, ip_field, port_field
                    )
                else:
                    if json_config:
                        raise ValueError(
                            f"Invalid JSON configuration in URL: {json_config}. Ensure it follows the format: 'url + whitespace + json=true&ip=<ip_field_name>&port=<port_field_name>'"
                        )
                    proxies = self.PROXY_REGEX.findall(text)

                self._status["total_proxy"] += len(proxies)
                validation_tasks = [
                    self.__validate_proxy(f"{self.config.prefix}{proxy}")
                    for proxy in proxies
                ]
                self._status["pending_sources"] -= 1
                self._status["valid_sources"] += 1
                return await asyncio.gather(*validation_tasks)

        except ClientError as e:
            self._status["pending_sources"] -= 1
            self._status["invalid_sources"] += 1
            print(f"Failed to fetch proxies from {url}: {e}")

    async def __load_proxies_from_cache(self):
        try:
            with open(self.config.proxy_cache_file, "r") as file:
                data = file.read().splitlines()
                print(f"Done restoring \033[33m{len(data)}\033[0m proxy from cache")
                return data
        except FileNotFoundError:
            return []

    async def __save_proxies_to_cache(self, proxies):
        try:
            with open(self.config.proxy_cache_file, "w") as file:
                file.write("\n".join(proxies))
                print(
                    f"Proxies saved to cache, in \033[33m{self.config.proxy_cache_file}\033[0m"
                )
        except Exception as e:
            print(f"Failed to save proxies to cache: {e}")

    def __is_cache_valid(self):
        try:
            cache_mod_time = datetime.fromtimestamp(
                os.path.getmtime(self.config.proxy_cache_file)
            )
            return datetime.now() - cache_mod_time < self.config.cache_duration
        except FileNotFoundError:
            return False

    async def get_valid_proxies(self):
        try:
            if self.config.cache_enabled and self.__is_cache_valid():
                print("Restoring proxies from cache")
                return await self.__load_proxies_from_cache()

            with open(self.config.proxy_sources_file) as file:
                source_urls = [
                    line for line in file.read().splitlines() if line.strip()
                ]
                self._status["total_sources"] = len(source_urls)

            self._monitor_status_task = asyncio.create_task(
                self.__print_monitoring_info()
            )
            tasks = [self.__fetch_proxies_from_source(url) for url in source_urls]
            results = await asyncio.gather(*tasks)

            self._monitor_status_task.cancel()
            await self.__print_monitoring_info(False)
            print()

            valid_proxies = []
            print(
                f"\033[36m{'Url':<40}\033[33m{'Total':<10}\033[32m{'✔ Valid':<10}\033[31m{'✗ Invalid':<10}\033[0m"
            )
            for group, url in zip(results, source_urls):
                valid = [proxy for proxy in group if proxy]
                valid_proxies.extend(valid)
                print(
                    f"\033[36m{url[:35]+'...':<40}\033[33m{len(group):<10}\033[32m{len(valid):<10}\033[31m{len(group) - len(valid):<10}\033[0m"
                )

            if self.config.cache_enabled:
                await self.__save_proxies_to_cache(valid_proxies)
            print(
                f"Now you have \033[33m{len(valid_proxies)} working proxy 🎉✨🎉\033[0m"
            )
            return valid_proxies

        except Exception as e:
            print(f"An exception occurred: {e}")

    async def close(self):
        await self._session.close()
        if self._monitor_status_task:
            self._monitor_status_task.cancel()


# Example usage
async def main():
    config = ProxyConfig(
        cache_enabled=True,
        enforce_unique_ip=False,
        cache_duration_minutes=5,
    )
    proxy_fetcher = ProxyFetcher(config)
    proxies = await proxy_fetcher.get_valid_proxies()
    print("Working Proxy List: ")
    print(proxies)
    await proxy_fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())
