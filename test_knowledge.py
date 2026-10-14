"""knowledge.py 自测：python test_knowledge.py（临时 DB，无网络）"""

import os
import tempfile

from knowledge import (OVERVIEW_CATEGORY, RECAP_CATEGORY,
                       KnowledgeBase, split_list)


def test_crud_and_search():
    tmp = tempfile.mkdtemp()
    kb = KnowledgeBase(os.path.join(tmp, "kb.db"))
    print("FTS5/trigram 可用：", kb.fts)

    a = kb.add("艾莉娅", category="人物", content="王国的年轻剑士，红发，性格倔强。",
               tags="主角, 剑士", aliases="小艾, 红发剑士", source="第一章")
    b = kb.add("黑森林", category="地点", content="王国北境的危险森林，藏有古代遗迹。",
               tags="危险地带")
    kb.add("王国", category="势力", content="故事的主舞台。")
    assert kb.count() == 3

    # 读
    ea = kb.get(a)
    assert ea["title"] == "艾莉娅" and ea["category"] == "人物"
    assert split_list(ea["aliases"]) == ["小艾", "红发剑士"]

    # 更新
    kb.update(a, content="王国的年轻剑士，红发，性格倔强，后来成为队长。", tags="主角, 剑士, 队长")
    assert "队长" in kb.get(a)["content"]

    # 中文关键字检索（子串）
    r = kb.search("剑士")
    assert any(e["id"] == a for e in r), [e["title"] for e in r]
    r = kb.search("森林")
    assert len(r) == 1 and r[0]["id"] == b
    # 正文内命中
    assert any(e["id"] == b for e in kb.search("遗迹"))

    # 类别筛选
    assert {e["title"] for e in kb.search("", category="人物")} == {"艾莉娅"}
    assert len(kb.list_entries(category="地点")) == 1
    assert set(kb.categories()) == {"人物", "地点", "势力"}

    # 精确检索：按名称/别名精确
    assert len(kb.search("艾莉娅", exact=True)) == 1
    assert len(kb.search("小艾", exact=True)) == 1          # 别名精确命中
    assert len(kb.search("艾", exact=True)) == 0            # 非完整名称不命中
    assert len(kb.search("艾", exact=False)) >= 1           # 子串命中

    # 短查询（<3 字）走 LIKE 兜底，仍能命中
    assert any(e["id"] == b for e in kb.search("森"))

    # 去重查找
    sim = kb.find_similar("艾莉娅")
    assert len(sim) == 1 and sim[0]["id"] == a
    assert kb.find_similar("红发剑士")                       # 传入名称命中既有别名
    assert kb.find_similar("全新人物", aliases="小艾")        # 传入别名命中既有别名
    assert not kb.find_similar("完全不相关的东西")

    # 删除
    kb.delete(b)
    assert kb.count() == 2 and kb.get(b) is None
    assert not kb.search("森林")   # FTS/LIKE 同步，删除后检索不到

    # 空名称保护
    try:
        kb.add("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("空名称应报错")

    kb.close()


def test_recap():
    tmp = tempfile.mkdtemp()
    kb = KnowledgeBase(os.path.join(tmp, "kb2.db"))
    kb.add("艾莉娅", category="人物", content="红发剑士")

    assert kb.get_recap() is None
    rid = kb.set_recap("第一章：艾莉娅离开王都。")
    r = kb.get_recap()
    assert r["id"] == rid and r["category"] == RECAP_CATEGORY
    assert "离开王都" in r["content"]

    # 再次设置只更新同一条，不新增
    rid2 = kb.set_recap("第一至二章：艾莉娅离开王都，进入黑森林。")
    assert rid2 == rid and kb.count() == 2
    assert "黑森林" in kb.get_recap()["content"]

    # 排除类别：普通词条列表里不含前情提要
    titles = [e["title"] for e in kb.list_entries(exclude_category=RECAP_CATEGORY)]
    assert titles == ["艾莉娅"], titles
    assert len(kb.list_entries()) == 2

    kb.delete(rid)
    assert kb.get_recap() is None
    kb.close()


def test_overview():
    tmp = tempfile.mkdtemp()
    kb = KnowledgeBase(os.path.join(tmp, "kb3.db"))
    kb.add("艾莉娅", category="人物", content="剑士")
    kb.set_recap("前情提要内容")

    assert kb.get_overview() is None and kb.overview_stale()      # 没有总览=需要生成
    fp = kb.overview_fingerprint()
    oid = kb.set_overview("这是世界观总览", fingerprint=fp)
    ov = kb.get_overview()
    assert ov["id"] == oid and ov["category"] == OVERVIEW_CATEGORY
    assert not kb.overview_stale()

    # 前情提要不参与指纹（它不是普通词条）
    kb.set_recap("改过的前情提要")
    assert not kb.overview_stale(), "前情提要变动不该让总览过期"

    # 普通词条变动 → 过期
    kb.add("黑森林", category="地点", content="森林")
    assert kb.overview_stale()
    kb.set_overview("更新后的总览", fingerprint=kb.overview_fingerprint())
    assert not kb.overview_stale()

    # normal_entries 排除总览与前情提要
    titles = [e["title"] for e in kb.normal_entries()]
    assert sorted(titles) == ["艾莉娅", "黑森林"], titles
    kb.close()


if __name__ == "__main__":
    test_crud_and_search()
    test_recap()
    test_overview()
    print("KNOWLEDGE TESTS PASSED")
