import requests
import json
import xml.etree.ElementTree as ET
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import os
import threading
from typing import Any, List, Optional, Set

# Global event to signal shutdown
shutdown_event = threading.Event()

# Global session for connection pooling
session = requests.Session()

# Regular expression to find proxies in IP:PORT format, with improved IP validation
PROXY_REGEX = re.compile(r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?):\d{1,5}\b')

# Headers for requests to mimic a browser
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# Add GitHub API token for a higher rate limit
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
API_HEADERS = HEADERS.copy()
if GITHUB_TOKEN:
    API_HEADERS['Authorization'] = f'token {GITHUB_TOKEN}'

def find_proxies_in_text(text: str) -> List[str]:
    """Finds all proxies in text content using a regular expression."""
    return PROXY_REGEX.findall(text)

def parse_json_recursively(element: Any, found_proxies: List[str]):
    """Recursively traverses a JSON structure to find proxies."""
    if isinstance(element, dict):
        for value in element.values():
            parse_json_recursively(value, found_proxies)
    elif isinstance(element, list):
        for item in element:
            parse_json_recursively(item, found_proxies)
    elif isinstance(element, str):
        found_proxies.extend(find_proxies_in_text(element))

def parse_xml_recursively(element: ET.Element, found_proxies: List[str]):
    """Recursively traverses an XML tree to find proxies in text nodes."""
    if element.text:
        found_proxies.extend(find_proxies_in_text(element.text))
    for child in element:
        parse_xml_recursively(child, found_proxies)

def fetch_and_parse_file(file_url: str, pbar: tqdm) -> List[str]:
    """Downloads and parses a file to find proxies."""
    if shutdown_event.is_set():
        return []

    try:
        response = session.get(file_url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        content = response.text
        if file_url.endswith('.json'):
            proxies: List[str] = []
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
        else:  # .txt and other formats
            return find_proxies_in_text(content)
    except requests.RequestException as e:
        pbar.set_description(f"Error fetching {file_url}: {e}")
    return []

def get_default_branch(user: str, repo: str, pbar: tqdm) -> Optional[str]:
    """Gets the default branch name for a repository."""
    api_url = f"https://api.github.com/repos/{user}/{repo}"
    try:
        response = session.get(api_url, headers=API_HEADERS, timeout=10)
        response.raise_for_status()
        repo_info = response.json()
        return repo_info.get('default_branch')
    except requests.RequestException as e:
        pbar.set_description(f"API error for {user}/{repo}: {e}")
    except json.JSONDecodeError:
        pbar.set_description(f"JSON decode error for {user}/{repo}")
    return None

def get_files_from_repo(repo_url: str, pbar: tqdm) -> List[str]:
    """Gets a list of files to parse from a GitHub repository, determining the default branch."""
    if shutdown_event.is_set():
        return []

    parts = repo_url.strip('/').split('/')
    if len(parts) < 2:
        pbar.set_description(f"Invalid repository URL: {repo_url}")
        return []
    user, repo = parts[-2], parts[-1]

    default_branch = get_default_branch(user, repo, pbar)

    if not default_branch:
        pbar.set_description(f"Could not determine default branch for {user}/{repo}, skipping")
        return []

    api_url = f"https://api.github.com/repos/{user}/{repo}/git/trees/{default_branch}?recursive=1"

    files_to_process = []
    try:
        response = session.get(api_url, headers=API_HEADERS, timeout=15)
        response.raise_for_status()

        data = response.json()
        if data.get('truncated'):
             pbar.set_description(f"Warning: File list for {user}/{repo} is truncated")

        for item in data.get('tree', []):
            if shutdown_event.is_set():
                break
            if item.get('type') == 'blob' and any(item.get('path','').endswith(ext) for ext in ['.txt', '.json', '.xml']):
                raw_url = f"https://raw.githubusercontent.com/{user}/{repo}/{default_branch}/{item['path']}"
                files_to_process.append(raw_url)
    except requests.RequestException as e:
        pbar.set_description(f"API error getting files for {user}/{repo}: {e}")
    except json.JSONDecodeError:
        pbar.set_description(f"JSON decode error for {user}/{repo}")

    return files_to_process

def process_repository(repo_url: str, pbar: tqdm) -> Set[str]:
    """Main function to process a single repository."""
    if shutdown_event.is_set():
        return set()

    user, repo = repo_url.strip('/').split('/')[-2:]
    pbar.set_description(f"Scanning {user}/{repo}")

    files = get_files_from_repo(repo_url, pbar)
    repo_proxies: Set[str] = set()

    if not files:
        pbar.set_description(f"No files found or skipped in {user}/{repo}")
        return repo_proxies

    with tqdm(total=len(files), desc=f"Files in {user}/{repo}", leave=False, unit="file") as file_pbar:
        for file_url in files:
            if shutdown_event.is_set():
                break
            proxies = fetch_and_parse_file(file_url, file_pbar)
            repo_proxies.update(proxies)
            file_pbar.update(1)

    pbar.set_description(f"Finished: {user}/{repo}, found {len(repo_proxies)} proxies")
    return repo_proxies

def main():
    """Main function to run the proxy scraper."""
    all_proxies: Set[str] = set()
    try:
        if not os.path.exists('repositories.txt'):
            print("Error: 'repositories.txt' not found.")
            print("Please create it and add repository URLs, one per line.")
            return

        with open('repositories.txt', 'r') as f:
            repo_urls = [line.strip() for line in f if line.strip()]

        if not repo_urls:
            print("'repositories.txt' is empty.")
            return

        print(f"Starting proxy collection from {len(repo_urls)} repositories...")
        if not GITHUB_TOKEN:
            print("Tip: For a higher GitHub API rate limit, set the GITHUB_TOKEN environment variable.")
            print("See: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token")
        print("Press Ctrl+C to stop.")

        with ThreadPoolExecutor(max_workers=10) as executor:
            with tqdm(total=len(repo_urls), desc="Overall Progress", unit="repo") as pbar:
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
                        pbar.set_description(f"Error processing {url}: {e}")
                        pbar.update(1)

    except KeyboardInterrupt:
        print("\n\nUser requested interruption. Shutting down...")
        shutdown_event.set()
    finally:
        if all_proxies:
            print(f"\nCollection complete. Found {len(all_proxies)} unique proxies.")
            print("Saving to 'proxies_output.txt'...")
            sorted_proxies = sorted(list(all_proxies))
            with open('proxies_output.txt', 'w') as f:
                for proxy in sorted_proxies:
                    f.write(proxy + '\n')
            print("Proxies saved successfully.")
        elif not shutdown_event.is_set():
            print("\nNo proxies found.")

if __name__ == "__main__":
    main()
