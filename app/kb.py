"""运营知识库：jieba 分词 + BM25 检索，内容存 MySQL，不需要向量库和 embedding 费用。"""
import re

import jieba
from rank_bm25 import BM25Plus
from sqlalchemy import delete, select

from .models import KBChunk, KBDocument

STOPWORDS = set("的 了 是 在 和 与 及 或 吗 呢 吧 啊 我 你 他 她 它 们 这 那 有 要 请 问 请问 什么 怎么 怎样 如何 多少 哪些 为什么 一个 一下 可以 需要".split())
CHUNK_SIZE = 500


def tokenize(text: str) -> list[str]:
    tokens = (t.strip().lower() for t in jieba.cut(text))
    return [t for t in tokens if t and t not in STOPWORDS and re.search(r"[\w一-鿿]", t)]


def split_chunks(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """按段落切块；超长段落按句子切开，尽量不把一句话拆成两半。"""
    chunks, current = [], ""
    for para in re.split(r"\n\s*\n|\r\n\s*\r\n", text):
        para = para.strip()
        if not para:
            continue
        pieces = [para] if len(para) <= size else [s for s in re.split(r"(?<=[。！？；.!?;\n])", para) if s.strip()]
        for piece in pieces:
            while len(piece) > size:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(piece[:size])
                piece = piece[size:]
            if len(current) + len(piece) + 1 > size and current:
                chunks.append(current)
                current = ""
            current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def add_document(db, title: str, text: str) -> KBDocument:
    chunks = split_chunks(text)
    if not chunks:
        raise ValueError("文档内容为空")
    doc = KBDocument(title=title[:200], chunk_count=len(chunks))
    db.add(doc)
    db.flush()
    db.add_all(KBChunk(document_id=doc.id, position=i, content=c) for i, c in enumerate(chunks))
    db.commit()
    return doc


def delete_document(db, doc_id: int) -> bool:
    doc = db.get(KBDocument, doc_id)
    if not doc:
        return False
    db.execute(delete(KBChunk).where(KBChunk.document_id == doc_id))
    db.delete(doc)
    db.commit()
    return True


def search(db, query: str, top_k: int = 4) -> list[dict]:
    rows = db.execute(
        select(KBChunk.id, KBChunk.content, KBDocument.title)
        .join(KBDocument, KBChunk.document_id == KBDocument.id)
    ).all()
    terms = tokenize(query)
    if not rows or not terms:
        return []
    corpus = [tokenize(r.content) for r in rows]
    scores = BM25Plus(corpus).get_scores(terms)
    # BM25Plus 对未命中的词也有基础分，所以只保留至少命中一个词的片段
    term_set = set(terms)
    ranked = sorted(
        (i for i, words in enumerate(corpus) if term_set & set(words)),
        key=lambda i: scores[i], reverse=True,
    )
    return [
        {"title": rows[i].title, "content": rows[i].content, "score": round(float(scores[i]), 3)}
        for i in ranked[:top_k]
    ]
