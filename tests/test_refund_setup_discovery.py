import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from customer_refunds import RefundError, SETTINGS
from refund_setup import discover_ms

ORG='00000000-0000-4000-8000-000000000001'
DOC='00000000-0000-4000-8000-000000000002'

class MoyskladDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def document(self,name='1010101010'):
        return {'id':DOC,'name':name,'organization':{'meta':{'href':'https://api.moysklad.ru/api/remap/1.2/entity/organization/'+ORG}}}

    async def test_exact_original_excludes_return_copies_and_only_reads(self):
        service=SimpleNamespace(ms=AsyncMock(return_value={'rows':[self.document(),self.document('копия - 1010101010')]}))
        with patch.dict(SETTINGS,{},clear=True):
            self.assertEqual(await discover_ms(service,'1010101010'),(ORG,DOC))
        self.assertEqual(service.ms.await_args.args[0],'GET')
        self.assertEqual(service.ms.await_count,1)

    async def test_ambiguous_or_truncated_search_cannot_bind_an_account(self):
        service=SimpleNamespace(ms=AsyncMock())
        for data in ({'rows':[]},{'rows':[self.document(),self.document()]},{'meta':{'size':101},'rows':[self.document()]}):
            service.ms.return_value=data
            with self.assertRaises(RefundError):await discover_ms(service,'1010101010')

    async def test_invalid_number_does_not_contact_moysklad(self):
        service=SimpleNamespace(ms=AsyncMock())
        with self.assertRaises(RefundError):await discover_ms(service,'../something')
        service.ms.assert_not_awaited()

class MoyskladConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_saved_when_order_missing_without_selecting_or_enabling_anything(self):
        import tempfile,json
        from pathlib import Path
        from refund_setup import save_ms_connection
        for rows in ([],[{'id':DOC},{'id':DOC}]):
            with tempfile.TemporaryDirectory() as directory:
                base=Path(directory)
                service=SimpleNamespace(ms=AsyncMock(side_effect=[{'rows':[{'id':ORG}]},{'rows':rows}]))
                result=await save_ms_connection(service,base,'fake-token','1010101010')
                self.assertEqual(result['search_matches'],len(rows))
                self.assertFalse(result['document_selected'])
                f=base/'.secrets/moysklad.json'
                self.assertEqual(json.loads(f.read_text())['token'],'fake-token')
                self.assertEqual(f.stat().st_mode & 0o777,0o600)
                self.assertFalse((base/'.secrets/refunds_config.json').exists())
                self.assertFalse((base/'.secrets/yandex_pay_refunds_enabled').exists())
                self.assertTrue(all(call.args[0]=='GET' for call in service.ms.await_args_list))

    async def test_bad_access_or_invalid_organizations_never_save_token(self):
        import tempfile
        from pathlib import Path
        from refund_setup import save_ms_connection
        for response in (RefundError('МойСклад: HTTP 401.'),{'rows':[]},{'rows':[{'id':'invalid'}]}):
            with tempfile.TemporaryDirectory() as directory:
                service=SimpleNamespace(ms=AsyncMock())
                if isinstance(response,Exception):service.ms.side_effect=response
                else:service.ms.return_value=response
                with self.assertRaises(RefundError):await save_ms_connection(service,Path(directory),'fake-token')
                self.assertFalse((Path(directory)/'.secrets/moysklad.json').exists())

    async def test_search_permission_error_does_not_discard_verified_connection(self):
        import tempfile
        from pathlib import Path
        from refund_setup import save_ms_connection
        with tempfile.TemporaryDirectory() as directory:
            service=SimpleNamespace(ms=AsyncMock(side_effect=[{'rows':[{'id':ORG}]},RefundError('МойСклад: HTTP 403.')]))
            result=await save_ms_connection(service,Path(directory),'fake-token','1010101010')
            self.assertIsNone(result['search_matches'])
            self.assertTrue((Path(directory)/'.secrets/moysklad.json').is_file())
