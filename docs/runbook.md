# Воспроизводимый bounded run

Нужны Python 3.12 и сетевой доступ к трём доменам из
[source-policy.md](source-policy.md). Скрипт не делает write-запросов, не
обходит CAPTCHA и не использует authentication.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
PYTHONPATH=src .venv/bin/python scripts/run_public_catalogs.py \
  --work-dir /tmp/supplier-data-pipeline-openfacts \
  --output-dir studies/openfacts-catalog-run-2026-08-10
PYTHONPATH=src .venv/bin/python scripts/render_catalog_charts.py \
  studies/openfacts-catalog-run-2026-08-10/results.json \
  --output-dir studies/openfacts-catalog-run-2026-08-10/graphs
```

`--work-dir` намеренно находится вне репозитория. В нём остаются исходные JSON
ответы и SQLite checkpoint, а в Git попадают только `results.json`, manifest и
SVG, построенные по результатам. При повторном запуске состав каталога может
измениться: сверяйте SHA-256, HTTP metadata, retrieval time и параметры из
manifest перед сравнением метрик.

Проверки кода:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
ruff check src tests scripts
PYTHONPATH=src .venv/bin/python -m compileall -q src scripts
```
