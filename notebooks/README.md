# Notebooks

Interactive Jupyter notebooks for email & SMS in AI agents.

## Run in the cloud (no setup)

Click any "Open in Colab" badge to run directly in Google Colab.

## Run locally

```bash
pip install jupyter commune-mail langchain-openai crewai openai
jupyter lab
```

## Notebooks

| File | Description | Framework |
|------|-------------|-----------|
| `quickstart.ipynb` | Create inbox, send email, read threads | Python |
| `langchain_customer_support.ipynb` | Full customer support agent | LangChain |
| `crewai_multi_agent_crew.ipynb` | Multi-agent email coordination | CrewAI |
| `structured_extraction.ipynb` | Auto-parse email fields to JSON | Any |
| `openai_agents_email.ipynb` | Email tools for OpenAI Agents | OpenAI Agents SDK |

## Requirements

All notebooks install their dependencies in the first cell. For cloud execution (Colab), no local setup is needed.

Set `COMMUNE_API_KEY` and `OPENAI_API_KEY` as environment variables or Colab secrets.
