"""Local chat routing safety tests; no live PBX changes."""
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.common import Error
from lib.config import load_config, validate_all
from lib.texting import ROUTE, change_chatplan, inspect_chatplan, route_expression


class TextingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'default.xml'
        self.original='<include><context name="public"><extension name="unrelated"><condition field="to" expression="^help$"><action application="log" data="notice"/></condition></extension></context></include>\n'
        self.path.write_text(self.original)
        self.change=Mock()
        self.change.file.side_effect=lambda path,data:Path(path).write_text(data)
        self.on={'enabled':True,'extensions':['1000','1005']}

    def test_scoped_route_and_disable_preserves_unrelated_entry(self):
        self.assertTrue(change_chatplan(self.path,'voip.example.com','internal',self.on,self.change))
        _,context,owned,other=inspect_chatplan(self.path)
        self.assertEqual(context[0].get('name'),ROUTE)
        self.assertEqual(other,[])
        expr=route_expression(owned)
        self.assertRegex('internal|1000|1005@voip.example.com',expr)
        self.assertNotRegex('external|1000|1005@voip.example.com',expr)
        self.assertNotRegex('internal|1000|9999@voip.example.com',expr)
        self.assertNotRegex('internal|1000|1005@other.example.com',expr)
        self.assertFalse(change_chatplan(self.path,'voip.example.com','internal',self.on,self.change))
        self.assertTrue(change_chatplan(self.path,'voip.example.com','internal',{'enabled':False,'extensions':[]},self.change))
        self.assertIsNone(inspect_chatplan(self.path)[2])
        self.assertIsNotNone(ET.parse(self.path).getroot().find(".//extension[@name='unrelated']"))

    def test_competing_chat_route_is_not_overwritten(self):
        before=self.original.replace('application="log" data="notice"','application="send" data="sip"')
        self.path.write_text(before)
        with self.assertRaisesRegex(Error,'Another SIP chat route'):
            change_chatplan(self.path,'voip.example.com','internal',self.on,self.change)
        self.assertEqual(self.path.read_text(),before)
        self.change.file.assert_not_called()
        with self.assertRaisesRegex(Error,'custom chat route'):
            change_chatplan(self.path,'voip.example.com','internal',{'enabled':False,'extensions':[]},self.change)

    def test_equivalent_legacy_route_is_adopted_and_can_be_disabled(self):
        from lib.texting import FIELD, expression
        root=ET.Element('include');ctx=ET.SubElement(root,'context',{'name':'public'})
        old=ET.SubElement(ctx,'extension',{'name':'local-extension-message'})
        condition=ET.SubElement(old,'condition',{'field':FIELD,'expression':expression('voip.example.com','internal',self.on['extensions'])})
        ET.SubElement(condition,'action',{'application':'send','data':'sip'})
        self.path.write_text(ET.tostring(root,encoding='unicode'))
        self.assertTrue(change_chatplan(self.path,'voip.example.com','internal',self.on,self.change))
        self.assertIsNotNone(inspect_chatplan(self.path)[2])
        self.assertTrue(change_chatplan(self.path,'voip.example.com','internal',{'enabled':False,'extensions':[]},self.change))
        self.assertEqual(len(inspect_chatplan(self.path)[3]),0)

    def test_older_site_defaults_off_and_requires_two_extensions(self):
        source=Path(__file__).resolve().parents[1]/'site.example.json'
        c=load_config(source)
        self.assertEqual(c['internal_chat'],{'enabled':False,'extensions':[]})
        c['internal_chat']={'enabled':True,'extensions':['1000']}
        with self.assertRaisesRegex(Error,'at least two'):
            validate_all(c)


if __name__=='__main__':unittest.main()
