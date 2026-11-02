import json
from pathlib import Path
import unittest
from assistant.core import digest,query_text
from assistant.knowledge import additional_documents

DATA=Path(__file__).parent/'assistant/data'


class AdditionalKnowledgeTests(unittest.TestCase):
    def test_cards_and_contacts_are_importable_without_attached_instructions(self):
        answers=json.loads((DATA/'knowledge_cards.json').read_text(encoding='utf-8'))
        contacts=json.loads((DATA/'contacts.json').read_text(encoding='utf-8'))
        docs=additional_documents(answers,contacts)
        self.assertEqual(len(answers['cards']),58)
        self.assertEqual(len(contacts['contacts']),154)
        self.assertEqual(len(docs),213)
        self.assertEqual(len({d['sha256'] for d in docs}),213)
        for source in answers['cards']:
            doc=next(d for d in docs if d['file']==f"unio-qa-{source['id']}.txt")
            text=doc['pages'][0]['text']
            self.assertIn(source['answer'],text)
            if source.get('not_verified'):self.assertIn(source['not_verified'],text)
            self.assertNotIn('https://t.me/',text)
            self.assertNotIn(source['rag_instruction'],text)
        self.assertTrue(any('Медпункт' in d['pages'][0]['text'] and 'отсутствуют' in d['pages'][0]['text'] for d in docs))

    def test_generated_corpus_matches_reference_and_owner_approval(self):
        primary=json.loads((DATA/'corpus.json').read_text(encoding='utf-8'))
        extra=json.loads((DATA/'additional_corpus.json').read_text(encoding='utf-8'))
        self.assertEqual(extra,additional_documents(json.loads((DATA/'knowledge_cards.json').read_text(encoding='utf-8')),
            json.loads((DATA/'contacts.json').read_text(encoding='utf-8'))))
        approval=json.loads((DATA/'activation.json').read_text(encoding='utf-8'))
        for d in primary+extra:
            self.assertEqual(approval['documents'][d['sha256']],digest(json.dumps({'pages':d['pages'],'title':d['title']},ensure_ascii=False,sort_keys=True)))

    def test_department_abbreviations_expand_for_search(self):
        self.assertIn('институт иностранных языков',query_text('почта ИИЯ'))
        self.assertIn('институт русского языка и словесности',query_text('контакты ИРЯС'))
