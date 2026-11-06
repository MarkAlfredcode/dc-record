# Run: python3 -I scripts/test_sam_file_state.py

import sys; sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import sam_file_state as s, triage_sam
assert s.run_status(False,None)=='restricted' and s.run_status(True,None)=='pending' and s.run_status(True,{'s':'retry'})=='pending'
assert s.run_status(True,{'s':'screened'})=='screened' and s.run_status(True,{'d':'x'})=='pending'
assert s.edition_status(False,True,None)=='queued' and s.edition_status(False,False,None)=='not_read' and s.edition_status(True,True,{})=='restricted'
assert s.edition_status(False,False,{'s':'failed'})=='failed' and s.edition_status(False,False,{'d':'x'})=='retry'
n={'attachments':[{'rid':'a','name':'A','status':'read','url':'u'},{'rid':'b','name':'B','status':'pending'},{'rid':'c','name':'C','status':'restricted'},{'rid':'d','name':'D','status':'pending'}]}
f,c=triage_sam.doc_view(n,{'a':{'s':'screened'},'b':{'s':'unsupported'}},set(),True,queue={'d':1})
print(c)
assert c['read']==1 and {u['reason'] for u in c['unread']}=={'no extractable text (video/CAD/etc.)','access-restricted on SAM.gov','queued, read in a later edition'}
print('ok')
