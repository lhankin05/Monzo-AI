# Monzo-AI

Local-first spending dashboard for Monzo and (if supported by the personal API) Monzo Flex.

## Initial setup

1. Install Python 3.11+.
2. Run `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and fill in your Monzo developer app client ID and secret locally. Never commit `.env`.
4. Run `uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload`.
5. Open http://127.0.0.1:8765/docs and test `/health` and `/config-status`.

The OAuth login flow and transaction/Flex discovery are not implemented yet. Monzo Flex support must be verified with the account before making dashboard assumptions. Keep tokens and raw transactions local; only anonymised aggregates may be sent to OpenAI in later phases.
