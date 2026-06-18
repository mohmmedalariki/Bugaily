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
SUBSCRIBERS_FILE = "subscribers.json"

# AI System Prompt — instructs the model to think like a hunter, not a journalist
SYSTEM_PROMPT = (
    "You are an elite bug bounty hunter and penetration tester with 10+ years of experience. "
    "Your audience is other professional bug bounty hunters who need actionable technical intelligence, "
    "NOT generic news summaries. You must respond with valid JSON ONLY. No markdown, no wrapping.\n\n"
    "Rules for writing the 'summary' field:\n"
    "- Write 4-6 detailed sentences.\n"
    "- Extract and mention specific technical details: CVE IDs, affected software versions, "
    "vulnerability classes (SSRF, IDOR, RCE, XSS, race condition, etc.), root causes, "
    "exploit chains, bypass techniques, and payload examples when available.\n"
    "- For writeups: describe the methodology step-by-step — what recon was done, what was found, "
    "how it was exploited, what the bypass or trick was, and the final impact.\n"
    "- For vulnerabilities/CVEs: state the affected product and version, the attack vector, "
    "whether auth is required, the CVSS score if available, and whether a PoC or patch exists.\n"
    "- For tools/methodology: explain what the tool does, how to use it in a hunt, "
    "and what it replaces or improves upon.\n"
    "- End each summary with a practical takeaway: what should a hunter DO with this information? "
    "e.g. 'Check your targets for X', 'Add Y to your wordlist', 'Test Z on endpoints that accept XML'.\n"
    "- NEVER write vague summaries like 'a vulnerability was found' or 'researchers discovered an issue'. "
    "Always be specific about WHAT, HOW, and WHY it matters to a hunter."
)


def build_summary_prompt(items, language):
    """Build the user-facing prompt with all article data."""
    prompt = (
        f"Analyze the following {len(items)} bug bounty / infosec items and produce actionable intelligence "
        f"in {language}.\n\n"
        f"Output MUST be valid JSON: a list of objects with these keys:\n"
        f"  - title: the article title\n"
        f"  - source: the feed source name\n"
        f"  - url: the article URL\n"
        f"  - summary: a detailed, technical, hunter-focused summary (4-6 sentences in {language}). "
        f"Include specific techniques, payloads, CVEs, tools, bypass methods, and a practical takeaway.\n"
        f"  - tag: one category from: Vulnerability, Writeup, Methodology, Tooling, Recon, "
        f"Exploit, Misconfiguration, News (translated to {language})\n\n"
        f"Here are the items:\n\n"
    )
    
    for i, item in enumerate(items):
        prompt += (
            f"--- Item {i+1} ---\n"
            f"Title: {item['title']}\n"
            f"Source: {item['source']}\n"
            f"URL: {item['url']}\n"
            f"Content:\n{item['content_preview']}\n\n"
        )
    
    return prompt


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


def load_subscribers() -> Dict[str, Any]:
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load subscribers: {e}")
    return {"last_update_id": 0, "chat_ids": []}


def save_subscribers(data: Dict[str, Any]):
    try:
        with open(SUBSCRIBERS_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save subscribers: {e}")


def update_subscribers(bot_token: str, dry_run: bool) -> Dict[str, Any]:
    sub_data = load_subscribers()
    if not bot_token:
        return sub_data
        
    offset = sub_data.get("last_update_id", 0) + 1
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={offset}&timeout=10"
    
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("ok"):
            updates = data.get("result", [])
            for update in updates:
                update_id = update["update_id"]
                sub_data["last_update_id"] = max(sub_data.get("last_update_id", 0), update_id)
                
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                
                if chat_id and chat_id not in sub_data["chat_ids"]:
                    logger.info(f"New subscriber found: {chat_id}")
                    sub_data["chat_ids"].append(chat_id)
                    
            if updates and not dry_run:
                save_subscribers(sub_data)
                
    except Exception as e:
        logger.error(f"Failed to get updates for subscribers: {e}")
        
    return sub_data


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
                    "content_preview": content[:3000], # Keep a large chunk for the AI to extract technical details
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
    prompt = build_summary_prompt(items, language)
    
    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT
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
    prompt = SYSTEM_PROMPT + "\n\n" + build_summary_prompt(items, language)
        
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={
                'response_mime_type': 'application/json',
                'temperature': 0.3
            }
        )
        
        content = response.text.strip()
        data = json.loads(content)
        
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    return v
            return [data]
        return data
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


def send_to_telegram(summaries: List[Dict[str, Any]], dry_run: bool, sub_data: Dict[str, Any]) -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set.")
        if not dry_run:
            return False
            
    chat_ids = sub_data.get("chat_ids", [])
    
    # Optional fallback to env var if no subscribers yet (for testing)
    env_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if env_chat_id:
        try:
            env_chat_id = int(env_chat_id)
            if env_chat_id not in chat_ids:
                chat_ids.append(env_chat_id)
        except ValueError:
            pass
            
    if not chat_ids:
        logger.info("No subscribers to send to.")
        return True
    
    # Build individual messages: 1 header + 1 per article
    date_str = datetime.now().strftime("%Y-%m-%d")
    header = f"🔒 <b>Bug Bounty & InfoSec Digest</b> — {date_str}\n\n📡 {len(summaries)} items today:"
    
    article_messages = []
    for idx, item in enumerate(summaries):
        tag = item.get("tag", "News")
        title = item.get("title", "No Title")
        source = item.get("source", "Unknown")
        summary = item.get("summary", "")
        url = item.get("url", "#")
        
        msg = (
            f"<b>{idx + 1}/{len(summaries)}</b> — <b>[{tag}]</b>\n\n"
            f"<a href='{url}'>{title}</a>\n"
            f"<i>via {source}</i>\n\n"
            f"{summary}"
        )
        article_messages.append(msg)
        
    all_messages = [header] + article_messages
        
    if dry_run:
        logger.info(f"DRY RUN: Sending {len(all_messages)} messages to {len(chat_ids)} users:")
        for m in all_messages:
            print("-" * 40)
            print(m)
        print("-" * 40)
        return True
        
    success_count = 0
    active_chat_ids = []
    
    for chat_id in chat_ids:
        send_ok = True
        for msg in all_messages:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            
            try:
                resp = requests.post(api_url, json=payload)
                if resp.status_code == 403:
                    logger.info(f"User {chat_id} blocked the bot. Removing from subscribers.")
                    send_ok = False
                    break
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Failed to send to {chat_id}: {e}")
                
        if send_ok:
            active_chat_ids.append(chat_id)
            success_count += 1
            logger.info(f"All {len(all_messages)} messages sent to {chat_id} successfully.")
            
    # Update subscribers if anyone was removed
    if len(active_chat_ids) != len(chat_ids) and not dry_run:
        sub_data["chat_ids"] = active_chat_ids
        save_subscribers(sub_data)
        
    return success_count > 0


def main():
    parser = argparse.ArgumentParser(description="Bug Bounty News Fetcher & Summarizer")
    parser.add_argument("--dry-run", action="store_true", help="Run without sending to Telegram or updating history")
    args = parser.parse_args()

    load_dotenv()
    
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    sub_data = update_subscribers(bot_token, args.dry_run)
    
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
    
    success = send_to_telegram(summaries, args.dry_run, sub_data)
    
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
