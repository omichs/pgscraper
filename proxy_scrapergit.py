import requests
import json
import xml.etree.ElementTree as ET
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import os
import threading

# Глобальный флаг для прерывания работы
shutdown_event = threading.Event()

# Регулярное выражение для поиска прокси в формате IP:PORT
PROXY_REGEX = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b')

# Заголовки для запросов, чтобы имитировать браузер
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def find_proxies_in_text(text):
    """Находит все прокси в текстовом содержимом с помощью регулярного выражения."""
    return PROXY_REGEX.findall(text)

def parse_json_recursively(element, found_proxies):
    """Рекурсивно обходит JSON-структуру в поиске прокси."""
    if isinstance(element, dict):
        for value in element.values():
            parse_json_recursively(value, found_proxies)
    elif isinstance(element, list):
        for item in element:
            parse_json_recursively(item, found_proxies)
    elif isinstance(element, str):
        found_proxies.extend(find_proxies_in_text(element))

def parse_xml_recursively(element, found_proxies):
    """Рекурсивно обходит XML-дерево в поиске прокси в текстовых узлах."""
    if element.text:
        found_proxies.extend(find_proxies_in_text(element.text))
    for child in element:
        parse_xml_recursively(child, found_proxies)

def fetch_and_parse_file(file_url):
    """Загружает и парсит файл для поиска прокси."""
    if shutdown_event.is_set():
        return []
        
    try:
        response = requests.get(file_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            content = response.text
            if file_url.endswith('.json'):
                proxies = []
                try:
                    json_data = json.loads(content)
                    parse_json_recursively(json_data, proxies)
                    return proxies
                except json.JSONDecodeError:
                    return find_proxies_in_text(content)
            elif file_url.endswith('.xml'):
                proxies = []
                try:
                    root = ET.fromstring(content)
                    parse_xml_recursively(root, proxies)
                    return proxies
                except ET.ParseError:
                    return find_proxies_in_text(content)
            else: # .txt и другие форматы
                return find_proxies_in_text(content)
    except requests.RequestException:
        pass
    return []

def get_default_branch(user, repo, pbar):
    """Получает имя ветки по умолчанию для репозитория."""
    api_url = f"https://api.github.com/repos/{user}/{repo}"
    try:
        response = requests.get(api_url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        repo_info = response.json()
        return repo_info.get('default_branch')
    except requests.RequestException:
        pbar.set_description(f"Ошибка получения инфо о {user}/{repo}")
    except json.JSONDecodeError:
        pbar.set_description(f"Ошибка JSON при получении инфо о {user}/{repo}")
    return None

def get_files_from_repo(repo_url, pbar):
    """Получает список файлов для парсинга из репозитория GitHub, определяя ветку по умолчанию."""
    if shutdown_event.is_set():
        return []

    parts = repo_url.strip('/').split('/')
    user, repo = parts[-2], parts[-1]
    
    default_branch = get_default_branch(user, repo, pbar)
    
    if not default_branch:
        pbar.set_description(f"Не удалось определить ветку для {user}/{repo}, пропуск")
        return []

    api_url = f"https://api.github.com/repos/{user}/{repo}/git/trees/{default_branch}?recursive=1"
    
    files_to_process = []
    try:
        response = requests.get(api_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        if data.get('truncated'):
             pbar.set_description(f"Внимание: список файлов для {user}/{repo} может быть неполным")

        if 'tree' not in data:
            pbar.set_description(f"Ошибка: не удалось получить дерево файлов для {user}/{repo}")
            return []

        for item in data.get('tree', []):
            if shutdown_event.is_set():
                break
            if item['type'] == 'blob' and any(item['path'].endswith(ext) for ext in ['.txt', '.json', '.xml']):
                raw_url = f"https://raw.githubusercontent.com/{user}/{repo}/{default_branch}/{item['path']}"
                files_to_process.append(raw_url)
    except requests.RequestException as e:
        pbar.set_description(f"Ошибка API GitHub для {user}/{repo}: {e}")
    except json.JSONDecodeError:
        pbar.set_description(f"Ошибка декодирования JSON для {user}/{repo}")
        
    return files_to_process

def process_repository(repo_url, pbar):
    """Основная функция для обработки одного репозитория."""
    if shutdown_event.is_set():
        return set()

    user, repo = repo_url.strip('/').split('/')[-2:]
    pbar.set_description(f"Сканирование {user}/{repo}")
    
    files = get_files_from_repo(repo_url, pbar)
    repo_proxies = set()
    
    if not files:
        pbar.set_description(f"Файлы не найдены или пропущены в {user}/{repo}")
        return repo_proxies

    with tqdm(total=len(files), desc=f"Файлы в {user}/{repo}", leave=False, unit="файл") as file_pbar:
        for file_url in files:
            if shutdown_event.is_set():
                break
            proxies = fetch_and_parse_file(file_url)
            repo_proxies.update(proxies)
            file_pbar.update(1)
            
    pbar.set_description(f"Завершено: {user}/{repo}, найдено {len(repo_proxies)} прокси")
    return repo_proxies

def main():
    """Главная функция для запуска сбора прокси."""
    all_proxies = set()
    try:
        if not os.path.exists('repositories.txt'):
            print("Ошибка: Файл 'repositories.txt' не найден.")
            print("Пожалуйста, создайте его и добавьте ссылки на репозитории.")
            return

        with open('repositories.txt', 'r') as f:
            repo_urls = [line.strip() for line in f if line.strip()]

        if not repo_urls:
            print("Файл 'repositories.txt' пуст.")
            return
            
        print(f"Начинается сбор прокси из {len(repo_urls)} репозиториев...")
        print("Для прекращения работы нажмите Ctrl+C")
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            with tqdm(total=len(repo_urls), desc="Общий прогресс", unit="репо") as pbar:
                future_to_url = {executor.submit(process_repository, url, pbar): url for url in repo_urls}
                
                for future in as_completed(future_to_url):
                    if shutdown_event.is_set():
                        for f in future_to_url:
                            f.cancel()
                        break
                    try:
                        result = future.result()
                        all_proxies.update(result)
                        pbar.update(1)
                    except Exception as e:
                        url = future_to_url[future]
                        pbar.set_description(f"Ошибка при обработке {url}: {e}")
                        pbar.update(1)

    except KeyboardInterrupt:
        print("\n\nПрерывание работы по запросу пользователя...")
        shutdown_event.set()
    finally:
        if all_proxies:
            print(f"\nСбор завершен. Найдено уникальных прокси: {len(all_proxies)}")
            print("Сохранение в файл 'proxies_output.txt'...")
            # Сортировка для упорядоченного вывода
            sorted_proxies = sorted(list(all_proxies))
            with open('proxies_output.txt', 'w') as f:
                for proxy in sorted_proxies:
                    f.write(proxy + '\n')
            print("Прокси успешно сохранены.")
        elif not shutdown_event.is_set():
            print("\nПрокси не найдены.")

if __name__ == "__main__":
    main()
