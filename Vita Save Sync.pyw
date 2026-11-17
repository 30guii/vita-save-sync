"""Abre a app sem janela de consola (duplo clique). Os erros ficam em app.log."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import PSVitaSaveSync  # noqa: E402

try:
    PSVitaSaveSync.main()
except Exception:
    PSVitaSaveSync.log.exception("A app fechou com um erro")
    raise
