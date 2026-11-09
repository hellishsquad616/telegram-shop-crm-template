import unittest
from customer_refunds import document_items, RefundError
from yandex_refunds import same_item, refund_body

class RealLogisticsTests(unittest.TestCase):
    def test_free_pickup_does_not_prevent_exact_goods_refund(self):
        order={'state':{'name':'Возврат'},'sum':799000}
        goods={'id':'goods','assortment':{'name':'DEMOZIP ZIP HOODIE BROWN (XS, коричневый)'},'price':799000,'quantity':1}
        pickup={'id':'pickup','assortment':{'name':'Самовывоз'},'price':0,'quantity':1}
        selected=document_items(order,[goods,pickup],'all')
        self.assertEqual(len(selected),1);self.assertEqual(selected[0]['unit_cents'],799000)
        pickup['assortment']['name']='Подарочный товар'
        self.assertEqual(len(document_items(order,[goods,pickup],'all')),2)
        pickup['assortment']['name']='Самовывоз';pickup['price']=-1
        with self.assertRaises(RefundError):document_items(order,[goods,pickup],'all')

    def test_cdek_aliases_use_paid_price_but_still_require_unique_position(self):
        self.assertTrue(same_item('Курьерская доставка CDEK','Курьер (СДЭК)'))
        self.assertFalse(same_item('Курьерская доставка CDEK','DEMO PUFFER RED, Размер=XS'))
        self.assertFalse(same_item('Курьерская доставка CDEK','Доставка Почтой России'))
        def item(pid,amount):return {'productId':pid,'title':'Курьер (СДЭК)','total':amount,'quantity':{'count':'1'},'receipt':{'tax':7,'paymentMethodType':1,'paymentSubjectType':1}}
        order={'orderAmount':'697.43','cart':{'total':{'amount':'697.43'},'items':[item('delivery','697.43')]}}
        selected=[{'name':'Курьерская доставка CDEK','unit_cents':69743,'quantity':1}]
        self.assertEqual(refund_body(order,selected)['refundAmount'],'697.43')
        selected[0]['unit_cents']=69744
        self.assertEqual(refund_body(order,selected)['refundAmount'],'697.43')
        selected[0]['unit_cents']=69743
        order['orderAmount']='1394.86';order['cart']['total']['amount']='1394.86';order['cart']['items'].append(item('another','697.43'))
        with self.assertRaises(RefundError):refund_body(order,selected)
