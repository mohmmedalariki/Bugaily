import argparse
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from time import mktime
from typing import List, Dict, Any

import feedparser
import requests
from dotenv import load_dotenv

# Load sources
try:
    from sources import FEEDS
except ImportError:
    print("Error: sources.py not found.")
    sys.exit(1)

# AI Providers
try:
    from groq import Groq
except ImportError:
    Groq = None

try:
    from google import genai
    from pydantic import BaseModel
except ImportError:
    genai = None


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
MAX_TOTAL_ITEMS = 5
MAX_PER_SOURCE = 2
HOURS_LOOKBACK = 36
HISTORY_FILE = "history.json"


def load_history() -> List[str]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load history: {e}")
            return []
    return []


def save_history(history: List[str]):
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save history: {e}")


def fetch_feeds(history: List[str]) -> List[Dict[str, Any]]:
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=HOURS_LOOKBACK)
    all_candidates = []

    for feed_info in FEEDS:
        url = feed_info["url"]
        source_name = feed_info["name"]
        logger.info(f"Fetching feed: {source_name} ({url})")
        
        try:
            # We use a custom User-Agent to avoid some basic bot blocks (e.g. reddit)
            feed = feedparser.parse(url, agent="BugBountyNewsBot/1.0")
            
            if feed.bozo and hasattr(feed, 'bozo_exception'):
                logger.warning(f"Feed {source_name} had bozo exception: {feed.bozo_exception}")
                # Sometimes it still parses fine despite bozo

            for entry in feed.entries:
                link = entry.get("link", "")
                title = entry.get("title", "No Title")
                
                if not link or link in history:
                    continue
                
                # Try to parse date
                published_tuple = entry.get("published_parsed") or entry.get("updated_parsed")
                if published_tuple:
                    dt = datetime.fromtimestamp(mktime(published_tuple), timezone.utc)
                    if dt < cutoff_time:
                        continue
                else:
                    # If we can't parse date, we might include it anyway if it's not in history
                    # But to be safe, let's just include it and trust history to deduplicate
                    logger.debug(f"No date parsed for {link}, including it.")
                
                content = ""
                if "content" in entry and len(entry.content) > 0:
                    content = entry.content[0].value
                elif "summary" in entry:
                    content = entry.summary
                elif "description" in entry:
                    content = entry.description
                    
                all_candidates.append({
                    "title": title,
                    "url": link,
                    "source": source_name,
                    "content_preview": content[:1500], # Keep a reasonable chunk for the AI to summarize
                    "published": entry.get("published", "")
                })

        except Exception as e:
            logger.error(f"Failed to process feed {source_name}: {e}")

    return all_candidates


def sample_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Shuffle first to ensure randomness
    random.shuffle(candidates)
    
    selected = []
    source_counts = {}
    
    for item in candidates:
        if len(selected) >= MAX_TOTAL_ITEMS:
            break
            
        src = item["source"]
        if source_counts.get(src, 0) < MAX_PER_SOURCE:
            selected.append(item)
            source_counts[src] = source_counts.get(src, 0) + 1
            
    return selected


def summarize_with_groq(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not Groq:
        raise Exception("Groq package not installed")
    
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise Exception("GROQ_API_KEY not set")
        
    client = Groq(api_key=api_key)
    
    language = os.getenv("SUMMARY_LANGUAGE", "English")
    prompt = f"Summarize the following bug bounty and infosec news items in {language}. Output MUST be valid JSON containing a list of objects with the keys: title, source, url, summary (a concise 1-2 sentence summary in {language}), tag (a single relevant category like 'Vulnerability', 'Writeup', 'News' translated to {language}). Here are the items:\n\n"
    
    for i, item in enumerate(items):
        prompt += f"Item {i+1}:\nTitle: {item['title']}\nSource: {item['source']}\nURL: {item['url']}\nContent Preview: {item['content_preview']}\n\n"
    
    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": "You are a cybersecurity expert that summarizes bug bounty news. You must respond with valid JSON ONLY. No markdown wrapping. Just the raw JSON array."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        model="llama-3.3-70b-versatile",
        temperature=0.3,
        response_format={"type": "json_object"}
    )
    
    content = response.choices[0].message.content.strip()
    try:
        data = json.loads(content)
        # Handle cases where the model wraps the list in an object (e.g. {"items": [...]})
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    return v
            return [data]
        return data
    except Exception as e:
        logger.error(f"Failed to parse Groq response as JSON: {content}\nError: {e}")
        raise Exception("Invalid JSON from Groq")


def summarize_with_gemini(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not genai:
        raise Exception("google-genai package not installed")
        
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY not set")
        
    client = genai.Client(api_key=api_key)
    
    language = os.getenv("SUMMARY_LANGUAGE", "English")
    prompt = f"Summarize the following bug bounty and infosec news items in {language}. For each item, provide a concise 1-2 sentence summary in {language} and a single relevant tag (e.g. 'Vulnerability', 'Writeup', 'News' translated to {language}).\n\n"
    for i, item in enumerate(items):
        prompt += f"Item {i+1}:\nTitle: {item['title']}\nSource: {item['source']}\nURL: {item['url']}\nContent Preview: {item['content_preview']}\n\n"
        
    class SummaryItem(BaseModel):
        title: str
        source: str
        url: str
        summary: str
        tag: str

    class ResponseSchema(BaseModel):
        items: list[SummaryItem]
        
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={
                'response_mime_type': 'application/json',
                'response_schema': ResponseSchema,
                'temperature': 0.3
            }
        )
        
        data = json.loads(response.text)
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Gemini API failed: {e}")
        raise


def get_summaries(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        logger.info("Attempting summarization with Groq (llama-3.3-70b-versatile)...")
        summaries = summarize_with_groq(items)
        # Ensure URLs are maintained correctly (AI might hallucinate or drop them)
        # It's better to just use the original URL and Source from the input
        # So we correlate by index if possible or just use what AI gave if it matches
        # For simplicity, we just trust the AI, but it's risky. Let's merge them back.
        if summaries and len(summaries) == len(items):
            for i in range(len(summaries)):
                summaries[i]["url"] = items[i]["url"]
                summaries[i]["source"] = items[i]["source"]
        return summaries
    except Exception as e:
        logger.warning(f"Groq failed: {e}. Falling back to Gemini...")
        try:
            summaries = summarize_with_gemini(items)
            if summaries and len(summaries) == len(items):
                for i in range(len(summaries)):
                    summaries[i]["url"] = items[i]["url"]
                    summaries[i]["source"] = items[i]["source"]
            return summaries
        except Exception as fallback_e:
            logger.error(f"Gemini fallback also failed: {fallback_e}")
            # Absolute fallback: just return the items without summary
            return [{"title": i["title"], "source": i["source"], "url": i["url"], "summary": "Failed to generate summary.", "tag": "News"} for i in items]


def send_to_telegram(summaries: List[Dict[str, Any]], dry_run: bool) -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        if not dry_run:
            return False
            
    date_str = datetime.now().strftime("%Y-%m-%d")
    message = f"🔒 <b>Bug Bounty & InfoSec Digest</b> - {date_str}\n\n"
    
    for item in summaries:
        tag = item.get("tag", "News")
        title = item.get("title", "No Title")
        source = item.get("source", "Unknown")
        summary = item.get("summary", "")
        url = item.get("url", "#")
        
        message += f"▪️ <b>[{tag}]</b> <a href='{url}'>{title}</a>\n"
        message += f"<i>via {source}</i>\n"
        message += f"{summary}\n\n"
        
    if dry_run:
        logger.info("DRY RUN: Telegram Message Payload:")
        print("-" * 40)
        print(message)
        print("-" * 40)
        return True
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
        logger.info("Message sent to Telegram successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to send to Telegram: {e}")
        if 'resp' in locals():
            logger.error(f"Response: {resp.text}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Bug Bounty News Fetcher & Summarizer")
    parser.add_argument("--dry-run", action="store_true", help="Run without sending to Telegram or updating history")
    args = parser.parse_args()

    load_dotenv()
    
    history = load_history()
    logger.info(f"Loaded {len(history)} items from history.")
    
    candidates = fetch_feeds(history)
    logger.info(f"Found {len(candidates)} new candidates in the last {HOURS_LOOKBACK} hours.")
    
    if not candidates:
        logger.info("No new items found. Exiting.")
        return
        
    selected_items = sample_candidates(candidates)
    logger.info(f"Sampled {len(selected_items)} items for summarization.")
    
    summaries = get_summaries(selected_items)
    
    success = send_to_telegram(summaries, args.dry_run)
    
    if success and not args.dry_run:
        # Update history
        for item in selected_items:
            history.append(item["url"])
        
        # Keep history file bounded (e.g. max 1000 items)
        if len(history) > 1000:
            history = history[-1000:]
            
        save_history(history)
        logger.info(f"History updated. Now contains {len(history)} items.")
    elif not success:
        logger.error("Failed to send message, history NOT updated.")

if __name__ == "__main__":
    main()
