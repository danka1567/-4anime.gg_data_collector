import requests
import json
from bs4 import BeautifulSoup
import re
from tqdm import tqdm
import time
import concurrent.futures
import asyncio
import aiohttp
import logging
from typing import List, Tuple, Optional

# Setup logging for errors
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('4anime_errors.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class AnimeExtractor:
    def __init__(self, api_key: str, max_workers: int = 20):
        self.api_key = api_key
        self.max_workers = max_workers
        self.session = None
        self.error_urls = []
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    def save_error_urls(self):
        """Save failed URLs to 4animerror.txt"""
        if self.error_urls:
            with open('4animerror.txt', 'w', encoding='utf-8') as f:
                for url in self.error_urls:
                    f.write(url + '\n')
            logging.info(f"Saved {len(self.error_urls)} error URLs to 4animerror.txt")
    
    async def extract_episodes_and_name(self, url: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        """Async version of episode extraction - handles both single and multiple episodes"""
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    self.error_urls.append(url)
                    return None, None, None
                    
                data = await response.json()
                html_content = data.get("html", "")
                
                if not html_content:
                    self.error_urls.append(url)
                    return None, None, None
                    
                soup = BeautifulSoup(html_content, 'html.parser')
                ep_items = soup.find_all('li', class_='ep-item')
                
                if not ep_items:
                    self.error_urls.append(url)
                    return None, None, None
                
                episode_numbers = []
                for item in ep_items:
                    data_id = item.get('data-id')
                    if data_id:
                        try:
                            episode_numbers.append(int(data_id))
                        except ValueError:
                            continue
                
                if not episode_numbers:
                    self.error_urls.append(url)
                    return None, None, None
                
                # Handle both single episode and multiple episodes
                if len(episode_numbers) == 1:
                    # Single episode case - use the same episode for first and last
                    first_ep = episode_numbers[0]
                    last_ep = episode_numbers[0]
                else:
                    # Multiple episodes case
                    first_ep = min(episode_numbers)
                    last_ep = max(episode_numbers)
                
                # Extract and keep original name format
                anime_name = None
                first_ep_link = ep_items[0].find('a')
                if first_ep_link and first_ep_link.get('href'):
                    href = first_ep_link.get('href')
                    match = re.search(r'/watch/([^?]+)', href)
                    if match:
                        anime_name = match.group(1) + '?'
                
                return first_ep, last_ep, anime_name
                
        except Exception as e:
            self.error_urls.append(url)
            logging.debug(f"Error processing {url}: {str(e)}")
            return None, None, None
    
    def get_tmdb_info_sync(self, anime_name: str) -> Tuple[Optional[int], Optional[int], str]:
        """Sync version for thread pool execution"""
        if not anime_name:
            return None, None, "Unknown Anime"
        
        clean_name = re.sub(r'-\d+\?$', '', anime_name)
        clean_name = clean_name.replace('-', ' ').strip()
        
        search_url = "https://api.themoviedb.org/3/search/tv"
        params = {
            'api_key': self.api_key,
            'query': clean_name,
            'language': 'en-US'
        }
        
        try:
            response = requests.get(search_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data['results']:
                series_data = data['results'][0]
                tmdb_id = series_data['id']
                year = None
                if series_data.get('first_air_date'):
                    year = int(series_data['first_air_date'].split('-')[0])
                return tmdb_id, year, series_data.get('name', clean_name.title())
            else:
                return None, None, clean_name.title()
                
        except Exception:
            return None, None, clean_name.title()

    async def process_urls_async(self, urls: List[str]) -> List[dict]:
        """Process URLs asynchronously for maximum speed"""
        successful_data = []
        
        # Process URLs in async batches
        for i in tqdm(range(0, len(urls), self.max_workers), desc="Processing URLs"):
            batch_urls = urls[i:i + self.max_workers]
            
            # Create tasks for current batch
            tasks = [self.extract_episodes_and_name(url) for url in batch_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results with thread pool for TMDB API calls
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                tmdb_tasks = []
                valid_results = []
                
                for j, result in enumerate(results):
                    if isinstance(result, Exception) or result[0] is None:
                        continue
                    
                    first_ep, last_ep, anime_name = result
                    if first_ep and last_ep and anime_name:
                        valid_results.append((batch_urls[j], first_ep, last_ep, anime_name))
                        # Submit TMDB search to thread pool
                        future = executor.submit(self.get_tmdb_info_sync, anime_name)
                        tmdb_tasks.append((future, first_ep, last_ep, anime_name))
                
                # Collect TMDB results
                for future, first_ep, last_ep, anime_name in tmdb_tasks:
                    try:
                        tmdb_id, year, title = future.result(timeout=10)
                        episode_offset = first_ep - 1
                        
                        # Format episodes string based on single or multiple episodes
                        if first_ep == last_ep:
                            episodes_str = f"{first_ep}"  # Single episode
                        else:
                            episodes_str = f"{first_ep}-{last_ep}"  # Multiple episodes
                        
                        series_entry = {
                            "serial_no": len(successful_data) + 1,
                            "name": anime_name,
                            "title": title,
                            "tmdb_id": tmdb_id,
                            "imdb_id": None,
                            "year": year,
                            "episodes": episodes_str,
                            "episode_offset": episode_offset
                        }
                        successful_data.append(series_entry)
                    except Exception:
                        continue
            
            # Small delay between batches to be respectful
            await asyncio.sleep(0.05)
        
        return successful_data

def main():
    """Main function with optimized async processing"""
    api_key = "6fad3f86b8452ee232deb7977d7dcf58"
    
    # Generate URLs from 1 to 200
    anime_urls = [f"https://4anime.gg/ajax/episode/list/{i}" for i in range(10000, 20001)]
    
    print(f"🚀 Starting ASYNC processing of {len(anime_urls)} URLs")
    print("⚡ Using concurrent processing for maximum speed")
    print("🎯 Handling both single and multiple episodes")
    print("📊 Progress will be shown below...")
    print("=" * 50)
    
    async def run_extraction():
        async with AnimeExtractor(api_key, max_workers=30) as extractor:
            # Process all URLs asynchronously
            series_data = await extractor.process_urls_async(anime_urls)
            
            # Save successful data
            if series_data:
                filename = "anime_series_data.json"
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(series_data, f, indent=4, ensure_ascii=False)
                
                # Save error URLs
                extractor.save_error_urls()
                
                print(f"\n✅ Successfully processed {len(series_data)} out of {len(anime_urls)} URLs")
                print(f"📁 Data saved to: {filename}")
                print(f"❌ Error URLs saved to: 4animerror.txt")
                
                # Show samples of both single and multiple episodes
                single_episodes = [s for s in series_data if '-' not in s['episodes']]
                multiple_episodes = [s for s in series_data if '-' in s['episodes']]
                
                print(f"📀 Single episodes: {len(single_episodes)}")
                print(f"🎬 Multiple episodes: {len(multiple_episodes)}")
                
                if single_episodes:
                    print(f"\n📋 Sample single episode:")
                    print(json.dumps(single_episodes[0], indent=2, ensure_ascii=False))
                if multiple_episodes:
                    print(f"\n📋 Sample multiple episodes:")
                    print(json.dumps(multiple_episodes[0], indent=2, ensure_ascii=False))
            else:
                print("❌ No data was successfully extracted")
                extractor.save_error_urls()
    
    # Run the async extraction
    asyncio.run(run_extraction())

# Fast synchronous version with single episode support
def fast_sync_version():
    """Faster synchronous version using ThreadPoolExecutor"""
    api_key = "6fad3f86b8452ee232deb7977d7dcf58"
    anime_urls = [f"https://4anime.gg/ajax/episode/list/{i}" for i in range(1, 201)]
    error_urls = []
    successful_data = []
    
    def process_single_url(url):
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            html_content = data.get("html", "")
            
            if not html_content:
                error_urls.append(url)
                return None
                
            soup = BeautifulSoup(html_content, 'html.parser')
            ep_items = soup.find_all('li', class_='ep-item')
            
            if not ep_items:
                error_urls.append(url)
                return None
            
            episode_numbers = []
            for item in ep_items:
                data_id = item.get('data-id')
                if data_id:
                    try:
                        episode_numbers.append(int(data_id))
                    except ValueError:
                        continue
            
            if not episode_numbers:
                error_urls.append(url)
                return None
            
            # Handle both single and multiple episodes
            if len(episode_numbers) == 1:
                first_ep = episode_numbers[0]
                last_ep = episode_numbers[0]
            else:
                first_ep = min(episode_numbers)
                last_ep = max(episode_numbers)
            
            anime_name = None
            first_ep_link = ep_items[0].find('a')
            if first_ep_link and first_ep_link.get('href'):
                href = first_ep_link.get('href')
                match = re.search(r'/watch/([^?]+)', href)
                if match:
                    anime_name = match.group(1) + '?'
            
            return first_ep, last_ep, anime_name, url
            
        except Exception:
            error_urls.append(url)
            return None
    
    print("🚀 Starting FAST SYNC processing...")
    
    # Use ThreadPoolExecutor for parallel processing
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        results = list(tqdm(
            executor.map(process_single_url, anime_urls),
            total=len(anime_urls),
            desc="Extracting URLs"
        ))
    
    # Process successful results and get TMDB info
    valid_results = [r for r in results if r is not None]
    
    print("🔍 Getting TMDB information...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        tmdb_tasks = {}
        for first_ep, last_ep, anime_name, url in valid_results:
            future = executor.submit(get_tmdb_info_sync, anime_name, api_key)
            tmdb_tasks[future] = (first_ep, last_ep, anime_name, url)
        
        for future in tqdm(concurrent.futures.as_completed(tmdb_tasks), 
                          total=len(tmdb_tasks), desc="TMDB Lookup"):
            first_ep, last_ep, anime_name, url = tmdb_tasks[future]
            try:
                tmdb_id, year, title = future.result()
                episode_offset = first_ep - 1
                
                # Format episodes string
                if first_ep == last_ep:
                    episodes_str = f"{first_ep}"  # Single episode
                else:
                    episodes_str = f"{first_ep}-{last_ep}"  # Multiple episodes
                
                series_entry = {
                    "serial_no": len(successful_data) + 1,
                    "name": anime_name,
                    "title": title,
                    "tmdb_id": tmdb_id,
                    "imdb_id": None,
                    "year": year,
                    "episodes": episodes_str,
                    "episode_offset": episode_offset
                }
                successful_data.append(series_entry)
            except Exception:
                error_urls.append(url)
    
    # Save results
    if successful_data:
        with open('anime_series_data.json', 'w', encoding='utf-8') as f:
            json.dump(successful_data, f, indent=4, ensure_ascii=False)
    
    # Save error URLs
    if error_urls:
        with open('4animerror.txt', 'w', encoding='utf-8') as f:
            for url in error_urls:
                f.write(url + '\n')
    
    print(f"\n✅ Processed {len(successful_data)} URLs successfully")
    print(f"❌ {len(error_urls)} URLs had errors (saved to 4animerror.txt)")
    
    # Show statistics
    single_episodes = [s for s in successful_data if '-' not in s['episodes']]
    multiple_episodes = [s for s in successful_data if '-' in s['episodes']]
    
    print(f"📀 Single episodes: {len(single_episodes)}")
    print(f"🎬 Multiple episodes: {len(multiple_episodes)}")

def get_tmdb_info_sync(anime_name: str, api_key: str) -> Tuple[Optional[int], Optional[int], str]:
    """Sync TMDB info function for thread pool"""
    if not anime_name:
        return None, None, "Unknown Anime"
    
    clean_name = re.sub(r'-\d+\?$', '', anime_name)
    clean_name = clean_name.replace('-', ' ').strip()
    
    search_url = "https://api.themoviedb.org/3/search/tv"
    params = {
        'api_key': api_key,
        'query': clean_name,
        'language': 'en-US'
    }
    
    try:
        response = requests.get(search_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data['results']:
            series_data = data['results'][0]
            tmdb_id = series_data['id']
            year = None
            if series_data.get('first_air_date'):
                year = int(series_data['first_air_date'].split('-')[0])
            return tmdb_id, year, series_data.get('name', clean_name.title())
        else:
            return None, None, clean_name.title()
            
    except Exception:
        return None, None, clean_name.title()

# Test function for single episode extraction
def test_single_episode():
    """Test the single episode extraction with your example data"""
    test_url = "https://4anime.gg/ajax/episode/list/10"  # Example single episode URL
    
    def extract_single_episode(url):
        try:
            response = requests.get(url, timeout=10)
            data = response.json()
            html_content = data.get("html", "")
            
            soup = BeautifulSoup(html_content, 'html.parser')
            ep_items = soup.find_all('li', class_='ep-item')
            
            episode_numbers = []
            for item in ep_items:
                data_id = item.get('data-id')
                if data_id:
                    episode_numbers.append(int(data_id))
            
            if len(episode_numbers) == 1:
                first_ep = episode_numbers[0]
                last_ep = episode_numbers[0]
                print(f"✅ Single episode detected: {first_ep}")
                return first_ep, last_ep
            else:
                print(f"❌ Expected single episode, got {len(episode_numbers)} episodes")
                return None, None
                
        except Exception as e:
            print(f"❌ Error: {e}")
            return None, None
    
    first_ep, last_ep = extract_single_episode(test_url)
    if first_ep and last_ep:
        print(f"🎯 Single episode extraction successful: {first_ep}")

if __name__ == "__main__":
    # Install required packages: pip install requests beautifulsoup4 tqdm aiohttp
    
    # Choose which version to run:
    # Option 1: Ultra-fast async version (recommended)
    main()
    
    # Option 2: Fast sync version (if you have issues with async)
    # fast_sync_version()
    
    # Option 3: Test single episode extraction
    # test_single_episode()
