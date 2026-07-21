# Reset local incident and knowledge-base data

Use this command when starting a completely fresh local scenario:

```powershell
python scripts\cleanup_data.py
```

Review the listed files, then type `DELETE` to confirm. The command removes:

- `data/incidents.db` and SQLite sidecar files
- All reviewed Markdown articles in `knowledge/approved/`

It does not modify application code, `.env`, watchlist configuration, or
`knowledge/mq_failure_patterns.md`.

For a deliberate non-interactive reset, use:

```powershell
python scripts\cleanup_data.py --yes
```
