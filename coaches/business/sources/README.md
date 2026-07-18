# Sources for the Business / Founder Coach

Drop files in this folder to ground the coach's questions in your own material:

- **Books / chapters** — paste useful sections as `.txt` or `.md`
- **Transcripts** — podcast notes, interview transcripts, meeting notes
- **Frameworks** — your own playbooks, decision rules, operating principles
- **Notes** — anything you want the coach to draw from

## Supported formats

- `.txt`
- `.md`

For PDFs or docx, extract the text first (copy-paste is fine). This keeps the tool dependency-free.

## How it works

On first run after you add/change files, the coach chunks them and computes embeddings via Ollama's `nomic-embed-text` model. Each chunk is cached in `.embeddings.json` keyed by file path + mtime, so subsequent runs are fast.

On every user message, the coach pulls the top few most relevant chunks and injects them as context. The coach is instructed to ground its questions in that material.

## Tips

- Prefer many smaller files over one giant one — makes retrieval more precise
- Keep individual chunks focused; one topic per paragraph works best
- If a file stops being useful, just delete it — the cache updates automatically
