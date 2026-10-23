# FlipSafe AI

FlipSafe is the secondhand selling desk for electronics. It inspects a device
photo, prices it from local comparison evidence, writes a copy-ready listing,
and reviews buyer messages for scams.

## Stack

- FastAPI + Uvicorn + PydanticAI slim + Pydantic + Pillow
- React + Vite + TypeScript
- `gpt-6-luna` through Portkey's Responses API at `https://api.portkey.ai/v1`

The vision worker uses PydanticAI's `OpenAIResponsesModel`. Listing and buyer
safety use the same Portkey Responses endpoint. No other model provider, web
search provider, marketplace posting, or payment system is used.

## Setup and run

Follow these steps from the project folder.

1. Create and activate a Python virtual environment:

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
```

After activation, your terminal should show `(.venv)`. Keep this terminal
activated while running the backend. Use `deactivate` when you are finished.

2. Create the local environment file:

```text
PORTKEY_API_KEY=your_portkey_key
```

Put that line in `.env`. You can copy `.env.example` first, then fill in your
Portkey key.

3. Install the backend packages:

```powershell
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
```

4. Install the frontend packages:

```powershell
cd frontend
npm install
cd ..
```

5. Start the backend:

```powershell
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

6. Open a second terminal, activate the virtual environment, start the frontend, and leave the backend terminal
running:

```powershell
.\.venv\Scripts\Activate.ps1
cd frontend
npm run dev
```

7. Open <http://127.0.0.1:5173> in your browser.

Vite proxies `/api` and `/data` to FastAPI. Fill in `PORTKEY_API_KEY` before
using live vision or listing generation. Local sample images, comparison math,
attack tests, and security screening work without a key.

## Local evidence

The app always uses the full synthetic comparison set in `data/full_data.json`.
Six public raster device demo photos are in `data/sample_images/`; their
synthetic seller notes and comparisons remain clearly labeled. Sources and
licenses are in `data/sample_images/SOURCES.md`. Eight buyer-message fixtures
are in `data/scam_test_cases.json`. The older
`data/test_data.json` remains as a standalone fixture for tests and examples,
but is not selected by the running app.

## Workflow and safety

The Boss delegates sequentially through:

1. image optimization and typed PydanticAI vision inspection;
2. comparison selection and Python price math;
3. listing compliance, listing copy, and buyer safety.

Stable prompts are loaded from `backend/prompts/` and cached before variable
inputs. The UI receives live agent/tool events over SSE and can cancel a run.
Polling remains as a client fallback if an SSE connection drops. Pricing stops
when condition or relevant comparison evidence is missing. Price tiers are
positive and ordered: walkaway floor ≤ quick sale ≤ fair market ≤ listing
anchor.

Input guardrails remove hidden Unicode/control text and block prompt injection,
secret seeking, phishing/payment traps, identity requests, and price
manipulation. Ordinary buyer questions and low offers are allowed. Secrets,
raw messages, prompts, private reasoning, and model output are not written to
the audit logs.

## Measurements and outputs

- `output/audit_trail.json` stores redacted run events, tools, short result summaries, errors, and stop reasons.
- `output/token_metrics.json` stores measured input, cached-input, cache-write, output tokens, and provider-reported USD costs when available; otherwise Tokenomics shows a clearly labeled planning estimate.
- `output/safety_events.json` stores triggered defense codes only.

The Tokenomics Lab compares optimized and unoptimized instruction runs and
projects average usage at 1, 100, and 10,000 monthly users. Measured Portkey
costs are used when available; otherwise the UI labels code-based planning
estimates. Run/session limits are six requests per agent, 12,000 tokens per run,
and code-defined spending caps.

Run checks:

```powershell
python -m unittest backend.test_tools
python -m compileall -q backend
cd frontend
npm run build
```
