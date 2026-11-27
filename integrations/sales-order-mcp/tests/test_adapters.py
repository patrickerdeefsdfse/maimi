import json
import httpx
import pytest
from order_mcp.config import Settings
from order_mcp.oracle import OracleGateway
class Var:
    value=None
    def getvalue(self):return self.value
class Lob:
    def write(self,value):self.value=value
    def close(self):pass
class Connection:
    def __init__(self,fail_at=None):self.calls=[];self.committed=False;self.rolled_back=False;self.fail_at=fail_at
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def cursor(self):return self
    def var(self,*args):return Var()
    def createlob(self,*args):return Lob()
    def commit(self):self.committed=True
    def rollback(self):self.rolled_back=True
    def callproc(self,name,keyword_parameters):
        stage=name.rsplit('.',1)[1];self.calls.append(stage)
        keyword_parameters['x_return_status'].value='E' if stage==self.fail_at else 'S'
        keyword_parameters['x_return_message'].value='test'
        if stage=='parse_xml_data':keyword_parameters['x_batch_number'].value=10
        if stage=='create_order':keyword_parameters['x_out_order_number'].value='1001'

@pytest.mark.parametrize('fail_at',[None,'parse_xml_data','validate_data','create_order'])
def test_oracle_three_steps_and_fail_fast(monkeypatch,fail_at):
    c=Connection(fail_at)
    g=OracleGateway(Settings(enable_write=True,user_code='TEST'))
    monkeypatch.setattr(g,'connect',lambda:c)
    if fail_at:
        with pytest.raises(ValueError):g.submit('<ROOT/>',lambda x:None)
        assert c.rolled_back and not c.committed
        if fail_at!='create_order':assert 'create_order' not in c.calls
    else:
        result=g.submit('<ROOT/>',lambda x:None)
        assert c.calls==['parse_xml_data','validate_data','create_order'] and c.committed
        assert result['order_number']=='1001'
