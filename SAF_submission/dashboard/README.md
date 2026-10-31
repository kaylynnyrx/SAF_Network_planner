# SAF Network Planner: how to run (Mac)

## First time only (~2 min)
Open **Terminal** (Cmd + Space, type "Terminal"), then paste these lines one at a time:
```
cd ~/Downloads/SAF_hackathon/app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Every time you start the app
```
cd ~/Downloads/SAF_hackathon/app
source .venv/bin/activate
export ANTHROPIC_API_KEY="sk-ant-..."     # only needed for the "Ask a what-if" box
streamlit run app.py
```
Your browser opens the app at http://localhost:8501. To stop it, press Ctrl + C in Terminal.

## Using it
- **Left sidebar:** pick a preset, or combine floods, droughts, refinery outages and demand changes, then click **Run scenario** (about 1 second).
- **Map:** hover over anything for details. Orange crosses = farms hit, grey triangle = refinery offline, red star = airport short of SAF.
- **Ask a what-if:** type a question in plain English. Claude proposes solver settings (shown so you can check them), the solver runs, and Claude explains using only the solver's numbers.
- **Tabs at the bottom:** facility locations and reasons, stress test, robustness, sanity checks, run history.

## Notes
- Works offline except the Claude box.
- The design (28 depots, 6 refineries) is fixed. Scenarios re-route material; they don't build anything new.
- To change the design itself (yield, demand, road factor), re-run the Colab notebook and replace `results/base.json` and `results/network.json`.
- Model used by the Claude box: `claude-sonnet-5-5`. Change it with `export CLAUDE_MODEL=...`.
