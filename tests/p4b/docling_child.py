"""Bounded real CPU extraction of a new synthetic PDF; no DB/queue credentials."""
import os,sys,json,socket
from pathlib import Path
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
sys.path.insert(0,str(R))
def deny(*a,**kw):raise RuntimeError('External sockets prohibited during real Docling smoke')
socket.socket.connect=deny;socket.create_connection=deny
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',QWEN_ENABLED='false')
from services.extraction.execution import execute_document_extraction
document,source,target=sys.argv[1:]
assert Path(source).resolve().is_relative_to(R)
assert Path(target).resolve().is_relative_to(R/'runs')
result=execute_document_extraction(document,Path(source),project_root=R,allow_vlm_inference=False,
 models_dir=Path('/mnt/d/Jonathan Files/AccountingIntelligence_SSD_Starter/AccountingIntelligence/models/docling'))
Path(target).write_text(json.dumps(result,indent=2))
sys.exit(0 if result['execution_status']=='EXTRACTED' else 1)
