"""Compatibility aliases for QCE v6.3.0 JSON -> ChatLab 0.37.1 QQ importer.
Preserves original IDs/content/fields. Never modifies source files or installed packages.
The tested QCE export uses id/type; the installed ChatLab importer expects
messageId/messageType and content.reply.referencedMessageId. Raw direct import loses IDs.
This is an offline adapter, NOT a QQ collector, credential reader or chat sender.
"""
from __future__ import annotations
import argparse,copy,hashlib,json
from pathlib import Path

MAX_BYTES=50*1024*1024
MAX_MESSAGES=200000

class CompatibilityError(ValueError): pass


def nonempty_string(value,label):
    if not isinstance(value,str) or not value or len(value)>512:
        raise CompatibilityError('INVALID_STRING_'+label)
    return value


def adapt(payload:dict, expected_peer:str, expected_account:str) -> tuple[dict,dict]:
    if not isinstance(payload,dict) or payload.get('metadata',{}).get('name')!='QQChatExporter':
        raise CompatibilityError('NOT_REVIEWED_QCE_JSON')
    info=payload.get('chatInfo',{})
    if info.get('peerUid')!=expected_peer:raise CompatibilityError('WRONG_CONVERSATION')
    if info.get('selfUin')!=expected_account:raise CompatibilityError('WRONG_ACCOUNT')
    if info.get('type')!='group':raise CompatibilityError('ONLY_GROUP_EXPORT_REVIEWED')
    records=payload.get('messages')
    if not isinstance(records,list) or len(records)>MAX_MESSAGES:raise CompatibilityError('MESSAGE_COUNT_BOUND')
    result=copy.deepcopy(payload);seen=set();reply_count=0;media_count=0
    for row in result['messages']:
        if not isinstance(row,dict):raise CompatibilityError('INVALID_MESSAGE_OBJECT')
        mid=nonempty_string(row.get('id'),'MESSAGE_ID')
        if mid in seen:raise CompatibilityError('DUPLICATE_ID_WITHIN_EXPORT')
        seen.add(mid)
        if row.get('messageId',mid)!=mid:raise CompatibilityError('CONFLICTING_MESSAGE_ID_ALIAS')
        row['messageId']=mid
        kind=nonempty_string(row.get('type'),'MESSAGE_TYPE')
        if row.get('messageType',kind)!=kind:raise CompatibilityError('CONFLICTING_TYPE_ALIAS')
        row['messageType']=kind
        ts=row.get('timestamp')
        if isinstance(ts,bool) or not isinstance(ts,int) or not 946684800000<=ts<4102444800000:
            raise CompatibilityError('EXPECTED_MILLISECOND_TIMESTAMP')
        sender=row.get('sender',{})
        nonempty_string(sender.get('uin') or sender.get('uid'),'SENDER_ID')
        content=row.get('content')
        if not isinstance(content,dict) or not isinstance(content.get('text',''),str):
            raise CompatibilityError('INVALID_CONTENT')
        elements=content.get('elements',[])
        if not isinstance(elements,list):raise CompatibilityError('INVALID_ELEMENTS')
        replies=[x.get('data',{}) for x in elements if isinstance(x,dict) and x.get('type')=='reply']
        if len(replies)>1:raise CompatibilityError('MULTIPLE_REPLY_PARENTS_REQUIRE_REVIEW')
        if replies:
            parent=replies[0].get('referencedMessageId') or replies[0].get('messageId')
            nonempty_string(parent,'REPLY_PARENT')
            existing=content.get('reply')
            if existing is not None and (not isinstance(existing,dict) or existing.get('referencedMessageId')!=parent):
                raise CompatibilityError('CONFLICTING_REPLY_ALIAS')
            content['reply']={**replies[0],'referencedMessageId':parent};reply_count+=1
        if content.get('resources'):media_count+=1
        # Sender names already in the export: preserve nickname vs group-card separately.
        if 'rawMessage' not in row:
            row['rawMessage']={'senderUin':sender.get('uin',''),'senderUid':sender.get('uid',''),
                'sendNickName':sender.get('nickname') or sender.get('name',''),
                'sendMemberName':sender.get('groupCard','')}
    summary={'messages':len(records),'unique_message_ids':len(seen),'reply_aliases':reply_count,
        'media_metadata_messages':media_count,'media_content_understood':False,
        'timestamp_unit_unchanged':'milliseconds','source_fields_preserved':True,
        'adapter_profile':'qce-6.3.0-to-chatlab-0.37.1-aliases-v1',
        'native_history_completeness_verified':False}
    return result,summary

