from app.universe_history import sync_universe_membership_history
from datetime import date


def test_incomplete_quote_snapshot_cannot_remove_members():
    class C:
        calls=[]
        rowcount=0
        def execute(self,sql,params):
            self.calls.append(sql)
            return self
    c=C()
    result=sync_universe_membership_history(c,'all_a',date(2026,9,7),['600664.SH'],
                                           source='longhuvip_composite',close_missing=False)
    assert result['closed']==0 and result['discarded_same_day']==0
    assert not any('DELETE' in s or 'closed_by' in s for s in c.calls)
