"""Explicit synthetic demo; never invokes a network adapter."""
import argparse
from pathlib import Path
from yingba.db import Store
from yingba.engine import Engine
from yingba.exchange import export
p=argparse.ArgumentParser();p.add_argument('--data',default='data/demo');args=p.parse_args()
s=Store(Path(args.data)/'workbench.db');e=Engine(s);task=e.create(15,'simulation');print(e.run(task));print(export(s,task,Path(args.data)/'exports'))
