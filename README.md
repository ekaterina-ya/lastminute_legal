# lastminute_legal

## Description
This project is a **Telegram bot** designed to analyze advertising creatives for compliance with **Russian advertising legislation**.  
It uses:
- **Gemini 2.5 Pro API** for legal reasoning
- **RAG (Retrieval-Augmented Generation)** for enhanced case-based analysis

The bot is capable of analyzing both text and image creatives, referencing actual decisions of the Federal Antimonopoly Service (FAS) to provide informed feedback.

---

## Features
- **Automated compliance check** of ads against Russian law
- **Support for text and image creatives**
- **RAG-powered contextual search** in FAS case database
- **Detailed legal explanations**
- **Interactive feedback collection from users**

---

## Architecture
[User] → [Telegram Bot API] → [Backend Logic] → [Gemini 2.5 Pro + RAG] → [Legal Analysis]
- **Frontend**: `telegram_bot.py` (manages Telegram interactions)
- **Backend**: `backend_logic.py` (Gemini API calls, semantic search, final analysis)

---
## Installation

1.  **Clone repository**
    ```bash
    git clone https://github.com/<your_username>/<repo_name>.git
    cd <repo_name>
    ```

2.  **Create virtual environment & install dependencies**
    ```bash
    python -m venv venv
    source venv/bin/activate    # (Linux/macOS)
    venv\Scripts\activate       # (Windows)
    pip install -r requirements.txt
    ```

3.  **Set environment variables**
    ```bash
    export TELEGRAM_BOT_TOKEN="your_token"
    export GEMINI_API_KEY="your_key"
    export RAG_DATA_PATH="data/rag_dataset.csv"
    export CORPUS_EMBEDDINGS_PATH="data/embeddings.npy"
    export PROMPT1_PREPROCESSING_PATH="prompts/preprocess.txt"
    export PROMPT2_ANALYSIS_PATH="prompts/analysis.txt"
    export EMBEDDING_MODEL="model_name"
    export GENERATIVE_MODEL="model_name"
    export RAG_TOP_N=5
    ```

## Usage

Run bot locally:
```bash
python telegram_bot.py
```
Bot will start listening for messages in Telegram.

## Legal Discaimer 

1. The bot is not affiliated with the Federal Antimonopoly Service (FAS), but it uses publicly available data provided by the FAS.
2. If you are bound by confidentiality obligations, using the bot may constitute a violation of such obligations.
3. The bot analyzes only the content of the material. It does not take into account the actual circumstances of its distribution (placement channels, licensing of your activities, etc.); therefore, the bot’s conclusion does not constitute a full legal consultation.
