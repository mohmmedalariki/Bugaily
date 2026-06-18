# Bugaily 🐛📰

Bugaily is a fully automated, serverless Telegram bot that aggregates, filters, and summarizes the latest Bug Bounty and InfoSec news. Built for elite bug bounty hunters, it uses AI (Groq / Gemini) to extract highly technical, actionable intelligence (payloads, bypasses, root causes) from various RSS feeds and broadcasts a daily digest directly to your Telegram app.

## ✨ Features

- **Automated RSS Aggregation**: Pulls news from top bug bounty platforms, community blogs, and subreddits (HackerOne, PortSwigger, Intigriti, r/bugbounty, and more).
- **Hacker-Focused AI Summarization**: Uses Groq (Llama-3) or Gemini to generate deeply technical 3-5 sentence summaries. It skips the generic fluff and focuses strictly on actionable intelligence like specific vulnerabilities, payloads, and methodologies.
- **Serverless & Free**: Runs entirely on GitHub Actions via a daily scheduled cron job. Zero hosting costs.
- **Stateful Broadcasting**: Automatically detects new Telegram users who start a conversation with the bot and saves their chat IDs to `subscribers.json`. Prevents duplicate news by tracking parsed URLs in `history.json`.
- **Multi-Language Support**: Set your preferred language (e.g., English, Arabic) in GitHub Variables, and the AI will translate the daily digest automatically.

## 🚀 Setup Guide

To deploy your own instance of Bugaily, follow these steps:

### 1. Fork the Repository
Click the **Fork** button at the top right of this repository to create your own copy.

### 2. Create a Telegram Bot
1. Open Telegram and search for [@BotFather](https://t.me/botfather).
2. Send `/newbot` and follow the steps to name your bot.
3. BotFather will give you a **Bot Token**. Save this for the next step.

### 3. Get AI API Keys (Free Tier)
- **Groq API Key**: Go to [Groq Console](https://console.groq.com/) and create a free API key (Primary).
- **Gemini API Key**: Go to [Google AI Studio](https://aistudio.google.com/) and create a free API key (Fallback).

### 4. Configure GitHub Secrets & Variables
Go to your forked repository on GitHub -> **Settings** -> **Secrets and variables** -> **Actions**.

**Create the following Secrets:**
- `TELEGRAM_BOT_TOKEN` : Your Telegram Bot Token.
- `GROQ_API_KEY` : Your Groq API Key.
- `GEMINI_API_KEY` : Your Gemini API Key.

**Create the following Variables:**
- `SUMMARY_LANGUAGE` : Your preferred language for the AI summaries (e.g., `English` or `Arabic`).

### 5. Give GitHub Actions Permission to Commit
Because Bugaily is serverless, it uses the repository itself as a database to store history and subscribers.
1. Go to repository **Settings** -> **Actions** -> **General**.
2. Scroll down to **Workflow permissions**.
3. Select **Read and write permissions** and click Save.

### 6. Start the Bot
1. Open Telegram and search for the bot you created.
2. Click **Start** (or send `/start`). The bot needs you to initiate the chat before it can send you messages.

### 7. Trigger the First Run
1. Go to the **Actions** tab in your GitHub repository.
2. Click on **Daily Bug Bounty Digest**.
3. Click the **Run workflow** button on the right side.
4. Wait 30-60 seconds, and you will receive your first Hacker-Focused Digest on Telegram! 

## 🏗️ How it Works

1. **Ingestion**: The script reads the `sources.py` file and fetches the last 36 hours of RSS items.
2. **Deduplication**: It compares URLs against `history.json` to ignore old news, capping at 5 items per run to stay within Telegram message limits.
3. **AI Generation**: It sends the articles to Groq (fallback to Gemini) with a highly specialized hacker prompt to extract actionable intelligence.
4. **Subscription Polling**: It polls the Telegram `getUpdates` API to find anyone who sent a `/start` message and adds them to `subscribers.json`.
5. **Broadcasting**: It iterates through `subscribers.json` and sends the HTML-formatted digest to everyone.
6. **State Sync**: The GitHub Action commits `history.json` and `subscribers.json` back to the `main` branch to persist state for tomorrow's run.
