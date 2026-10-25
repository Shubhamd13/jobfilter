# Screenshots

These images come from a throwaway demo database, never from a real one: public postings
fetched with `jobfilter check`, screened with `profile.example.json`, read by the local
model, and a few made-up applications with placeholder resume files.

To take them again:

```bash
export JOBFILTER_DB=/tmp/demo.db JOBFILTER_PROFILE=$PWD/profile.example.json
jobfilter check urls.txt --expand
jobfilter check 'https://ats.rippling.com/lyte/jobs' 'https://apply.workable.com/octavian-robotics/'
jobfilter read                              # needs Ollama; screens first
jobfilter serve --port 8765                 # then mark a few postings Applied in the page
google-chrome --headless=new --hide-scrollbars --window-size=1440,900 \
    --virtual-time-budget=8000 --screenshot=inbox.png 'http://127.0.0.1:8765/#inbox'
```

The extension badge is taken on the hand-built LinkedIn fixture through `tests/cdp.py`,
the same way the end-to-end tests load the extension. Crop to the content and reduce to
256 colors (`convert in.png -colors 256 PNG8:out.png`) to keep each file small.
