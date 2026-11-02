"""Pure conversion of owner-supplied references. No attached instructions run."""
from .core import digest
from .sources import public_source_url


def document(kind, key, title, text, url, locator, **metadata):
    return dict(file=f'unio-{kind}-{key}.txt',sha256=digest(f'unio:{kind}:{key}'),title=title,
                pages=[dict(text=text,locator=locator,ocr=False)],source_url=public_source_url(url),
                review_status='owner_test_approval',**metadata)


def additional_documents(answers, contacts):
    result=[]
    for card in answers['cards']:
        sources=card.get('official_sources') or []
        url=next((public_source_url(s.get('url')) for s in sources if public_source_url(s.get('url'))),None)
        # Telegram messages are examples of questions, not factual evidence.
        # rag_instruction is intentionally not imported as an instruction.
        text='\n'.join([f"Справочная карточка UNIO. Категория: {card.get('category','')}",
            f"Вопрос: {card['question']}",f"Ответ из предоставленного справочника: {card['answer']}",
            f"Статус подтверждения в справочнике: {card.get('evidence_status','unverified')}",
            f"Не подтверждено / ограничения: {card.get('not_verified') or 'Отдельно не указаны; сохраняются условия ответа.'}",
            f"Контекст для уточнения: {card.get('clarifying_question_context') or 'Не указан'}",
            f"Дата составления карточки: {card.get('checked_on') or answers.get('date','Не указана')}"])
        result.append(document('qa',card['id'],'Справка UNIO: '+card['question'],text,url,
            f"Сценарий {card['id']}",evidence_status=card.get('evidence_status'),official_sources=sources))
    for contact in contacts['contacts']:
        text='\n'.join([f"Контакты ВГСПУ. Подразделение: {contact['department']}",
            f"Электронная почта / email: {contact['email']}",
            f"Примечание и границы компетенции: {contact.get('scope_note') or 'Не указаны'}",
            f"Дата записи в предоставленном реестре: {contact.get('last_checked','Не указана')}"])
        result.append(document('contact',contact['id'],'Контакты: '+contact['department'],text,
            contact.get('source_url'),f"Контактная запись {contact['id']}"))
    missing=contacts.get('meta',{}).get('directory_departments_without_email') or []
    if missing:
        text='Электронная почта в предоставленном реестре не указана для подразделений: '+ '; '.join(missing)+'. Адреса для них в реестре отсутствуют; контакт требуется уточнить на официальной странице подразделений.'
        result.append(document('contact','without-email','Подразделения без указанного email',text,
            'https://www.vspu.ru/departments','Примечание к реестру контактов'))
    return result
