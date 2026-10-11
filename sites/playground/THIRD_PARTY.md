# Browser runtime notices

Sluice code is MIT licensed. The browser runtime additionally distributes:

- [Pyodide](https://github.com/pyodide/pyodide), Mozilla Public License 2.0.
- [CPython](https://docs.python.org/3/license.html), Python Software Foundation licence and included notices.
- NumPy, pandas, Pydantic, PyYAML, openpyxl and their dependencies, under their respective upstream licences.

Python wheels are retained as published, including their `.dist-info` licence files and metadata.
Pyodide's MPL applies to its covered files; this app does not modify their code. Its source is available from
[the pinned Pyodide release](https://github.com/pyodide/pyodide/tree/314.0.7).
The download manifest `vendor/sources.json` lists the official URLs and SHA-256 of each shipped asset.
