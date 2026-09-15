from datetime import date
from unittest import TestCase
from app.short_term_lanes.reviews import validate


class ReviewTests(TestCase):
    def example(self):
        return {'symbol':'002170.SZ','name':'芭田股份','business':'复合肥','risk':'达产风险','conclusion':'观察',
                'selection':{'origin':'scan','why_now':'本轮入选','question':'是否有业绩支撑','disposition':'retain_watch'},
                'sources':[{'url':'https://static.cninfo.com.cn/test.pdf','published_date':'2026-09-02'}]}

    def test_requires_business_and_downside(self):
        r=self.example();r.pop('risk')
        with self.assertRaises(ValueError):validate(r,date(2026,9,4))

    def test_rejects_future_and_random_blog(self):
        r=self.example();r['sources'][0]['published_date']='2026-09-05'
        with self.assertRaises(ValueError):validate(r,date(2026,9,4))
        r=self.example();r['sources'][0]['url']='https://blog.test/rumor'
        with self.assertRaises(ValueError):validate(r,date(2026,9,4))

    def test_primary_filing_accepted(self):
        validate(self.example(),date(2026,9,4))
