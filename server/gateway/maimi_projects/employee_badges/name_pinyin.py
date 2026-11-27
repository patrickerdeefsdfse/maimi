"""Offline Mandarin name romanization for badges without employee-supplied English."""
from base64 import b85decode
from functools import lru_cache
from struct import unpack_from
from zlib import decompress

from .name_pinyin_data import FIRST, PACKED_READINGS, SYLLABLES

# Conventional Mandarin readings of common compound or polyphonic surnames.
COMPOUND = {
    '欧阳': 'Ouyang', '歐陽': 'Ouyang', '司马': 'Sima', '司馬': 'Sima',
    '诸葛': 'Zhuge', '諸葛': 'Zhuge', '上官': 'Shangguan', '皇甫': 'Huangfu',
    '司徒': 'Situ', '司空': 'Sikong', '令狐': 'Linghu', '慕容': 'Murong',
    '尉迟': 'Yuchi', '尉遲': 'Yuchi', '长孙': 'Zhangsun', '長孫': 'Zhangsun',
    '万俟': 'Moqi', '萬俟': 'Moqi', '单于': 'Chanyu', '單于': 'Chanyu',
    '澹台': 'Tantai', '澹臺': 'Tantai', '夏侯': 'Xiahou', '宇文': 'Yuwen',
    '公孙': 'Gongsun', '公孫': 'Gongsun', '公羊': 'Gongyang', '闻人': 'Wenren',
    '聞人': 'Wenren', '淳于': 'Chunyu', '南宫': 'Nangong', '南宮': 'Nangong',
    '司寇': 'Sikou', '仲孙': 'Zhongsun', '仲孫': 'Zhongsun', '公冶': 'Gongye',
    '东方': 'Dongfang', '東方': 'Dongfang', '独孤': 'Dugu', '獨孤': 'Dugu',
    '轩辕': 'Xuanyuan', '軒轅': 'Xuanyuan', '端木': 'Duanmu', '西门': 'Ximen',
    '西門': 'Ximen', '第五': 'Diwu', '钟离': 'Zhongli', '鍾離': 'Zhongli',
}
SURNAMES = {
    '单': 'Shan', '單': 'Shan', '区': 'Ou', '區': 'Ou', '仇': 'Qiu',
    '解': 'Xie', '查': 'Zha', '曾': 'Zeng', '缪': 'Miao', '繆': 'Miao',
    '朴': 'Piao', '翟': 'Zhai', '盖': 'Ge', '蓋': 'Ge', '任': 'Ren',
}


@lru_cache(maxsize=1)
def _readings():
    return decompress(b85decode(PACKED_READINGS))


def _syllable(character):
    offset = (ord(character) - FIRST) * 2
    data = _readings()
    if offset < 0 or offset + 2 > len(data):
        return ''
    return SYLLABLES[unpack_from('>H', data, offset)[0]]


def name_pinyin(name):
    """Return surname then given name, or an empty string for incomplete readings."""
    if not name:
        return ''
    syllables = [_syllable(character) for character in name]
    if not all(syllables):
        return ''
    surname = COMPOUND.get(name[:2]) if len(name) > 2 else None
    given = syllables[2:] if surname else syllables[1:]
    surname = surname or SURNAMES.get(name[0]) or syllables[0].capitalize()
    if not given:
        return surname
    parts = [given[0].capitalize()]
    for syllable in given[1:]:
        if syllable[0] in 'aeo':
            parts.append("'")
        parts.append(syllable)
    return surname + ' ' + ''.join(parts)
