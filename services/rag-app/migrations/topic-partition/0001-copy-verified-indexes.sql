-- Non-destructive topic partition migration.
--
-- Preconditions:
--   * source files were copied to the target paths;
--   * old and new SHA-256 values were verified equal;
--   * rag_documents has the topic-partition columns added by `rag.py init`.
--
-- This migration does not delete or alter legacy documents/chunks. Because the
-- bytes, chunker, embedding model, and vector dimensions are unchanged, it
-- copies the already validated chunks/vectors into independently addressable
-- version-2 document records at the new source paths.

BEGIN;

CREATE TEMPORARY TABLE topic_partition_map (
    old_source_path text PRIMARY KEY,
    new_source_path text NOT NULL UNIQUE,
    knowledge_base text NOT NULL,
    source_relative_path text NOT NULL
) ON COMMIT DROP;

INSERT INTO topic_partition_map (
    old_source_path,
    new_source_path,
    knowledge_base,
    source_relative_path
)
VALUES
    (
        '/home/ubuntu/services/rag-app/data/cpp-study.md',
        '/home/ubuntu/Tech/资料/cpp-study.md',
        'tech',
        '资料/cpp-study.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/server-plan.md',
        '/home/ubuntu/Tech/资料/server-plan.md',
        'tech',
        '资料/server-plan.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/investment.md',
        '/home/ubuntu/金融与投资/资料/investment.md',
        'finance',
        '资料/investment.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/25分鐘講完荷馬史詩《奧德賽》,完整劇情一口氣看懂 - 這部西方文學的開山之作,到底有多精彩--7bcb2073.md',
        '/home/ubuntu/文学与文化/视频转录/25分鐘講完荷馬史詩《奧德賽》,完整劇情一口氣看懂 - 這部西方文學的開山之作,到底有多精彩--7bcb2073.md',
        'literature-culture',
        '视频转录/25分鐘講完荷馬史詩《奧德賽》,完整劇情一口氣看懂 - 這部西方文學的開山之作,到底有多精彩--7bcb2073.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/【零到全栈】5.1-什么是API--facca39f.md',
        '/home/ubuntu/Tech/视频转录/【零到全栈】5.1-什么是API--facca39f.md',
        'tech',
        '视频转录/【零到全栈】5.1-什么是API--facca39f.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/分享一个用领英找工作的野路子-如何要内推--29c1a2d8.md',
        '/home/ubuntu/职业发展/视频转录/分享一个用领英找工作的野路子-如何要内推--29c1a2d8.md',
        'career',
        '视频转录/分享一个用领英找工作的野路子-如何要内推--29c1a2d8.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/SpaceX上市-这是千载难逢的财富盛宴,还是史上最大的陷阱-普通人到底该不该上车- - SpaceX IPO - 马斯克 - 美股 - 纳斯达克 - 算力 - 星链 - 老周横眉--bb8f77cb.md',
        '/home/ubuntu/思想与政治/老周横眉/SpaceX上市-这是千载难逢的财富盛宴,还是史上最大的陷阱-普通人到底该不该上车- - SpaceX IPO - 马斯克 - 美股 - 纳斯达克 - 算力 - 星链 - 老周横眉--bb8f77cb.md',
        'thought-politics',
        '老周横眉/SpaceX上市-这是千载难逢的财富盛宴,还是史上最大的陷阱-普通人到底该不该上车- - SpaceX IPO - 马斯克 - 美股 - 纳斯达克 - 算力 - 星链 - 老周横眉--bb8f77cb.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/SpaceX上市一周-暴涨背后,真正的疯狂才刚开始 - 老周快评 - SpaceX IPO - SPCX - 马斯克 - 星链 - xAI - 老周横眉--22a006dc.md',
        '/home/ubuntu/思想与政治/老周横眉/SpaceX上市一周-暴涨背后,真正的疯狂才刚开始 - 老周快评 - SpaceX IPO - SPCX - 马斯克 - 星链 - xAI - 老周横眉--22a006dc.md',
        'thought-politics',
        '老周横眉/SpaceX上市一周-暴涨背后,真正的疯狂才刚开始 - 老周快评 - SpaceX IPO - SPCX - 马斯克 - 星链 - xAI - 老周横眉--22a006dc.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/X Money正式上线!马斯克的超级应用梦,离微信还差三座大山。打造美国版微信为何这么难- - 老周横眉--47881f8a.md',
        '/home/ubuntu/思想与政治/老周横眉/X Money正式上线!马斯克的超级应用梦,离微信还差三座大山。打造美国版微信为何这么难- - 老周横眉--47881f8a.md',
        'thought-politics',
        '老周横眉/X Money正式上线!马斯克的超级应用梦,离微信还差三座大山。打造美国版微信为何这么难- - 老周横眉--47881f8a.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/“牛市股神” 为什么很多都活不到最后-如何一边赚钱,一边守住钱。 - 财富心理学 - 投资理财 - 赚钱 - 美股 - 纳斯达克 - A股 - 指数基金 - 财富自由 - 老周横眉--73ddcfbf.md',
        '/home/ubuntu/思想与政治/老周横眉/“牛市股神” 为什么很多都活不到最后-如何一边赚钱,一边守住钱。 - 财富心理学 - 投资理财 - 赚钱 - 美股 - 纳斯达克 - A股 - 指数基金 - 财富自由 - 老周横眉--73ddcfbf.md',
        'thought-politics',
        '老周横眉/“牛市股神” 为什么很多都活不到最后-如何一边赚钱,一边守住钱。 - 财富心理学 - 投资理财 - 赚钱 - 美股 - 纳斯达克 - A股 - 指数基金 - 财富自由 - 老周横眉--73ddcfbf.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/中共为何不让人民买美股-A股为何不能碰-富途、老虎被重罚背后的逻辑 - 纳斯达克 - 香港汇丰 - QDII - 投资策略 - 券商开户 - 老周横眉--a9401bbf.md',
        '/home/ubuntu/思想与政治/老周横眉/中共为何不让人民买美股-A股为何不能碰-富途、老虎被重罚背后的逻辑 - 纳斯达克 - 香港汇丰 - QDII - 投资策略 - 券商开户 - 老周横眉--a9401bbf.md',
        'thought-politics',
        '老周横眉/中共为何不让人民买美股-A股为何不能碰-富途、老虎被重罚背后的逻辑 - 纳斯达克 - 香港汇丰 - QDII - 投资策略 - 券商开户 - 老周横眉--a9401bbf.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/中国实现火箭回收-承认中国的成就,不等于认可这个制度。 - 长征十号乙 - 中共体制 - 制度优势 - 反共 - 老周横眉--213074c3.md',
        '/home/ubuntu/思想与政治/老周横眉/中国实现火箭回收-承认中国的成就,不等于认可这个制度。 - 长征十号乙 - 中共体制 - 制度优势 - 反共 - 老周横眉--213074c3.md',
        'thought-politics',
        '老周横眉/中国实现火箭回收-承认中国的成就,不等于认可这个制度。 - 长征十号乙 - 中共体制 - 制度优势 - 反共 - 老周横眉--213074c3.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/为什么网上那么多人不讲逻辑-拆解评论区里的7大逻辑谬误 - 老周横眉--3ef9ae54.md',
        '/home/ubuntu/思想与政治/老周横眉/为什么网上那么多人不讲逻辑-拆解评论区里的7大逻辑谬误 - 老周横眉--3ef9ae54.md',
        'thought-politics',
        '老周横眉/为什么网上那么多人不讲逻辑-拆解评论区里的7大逻辑谬误 - 老周横眉--3ef9ae54.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/泰山围上135公里刀片铁丝网-祖国大好河山,是能让你免费看的吗- - 中国旅游乱象 - 中共腐败 - 老周横眉--92319ced.md',
        '/home/ubuntu/思想与政治/老周横眉/泰山围上135公里刀片铁丝网-祖国大好河山,是能让你免费看的吗- - 中国旅游乱象 - 中共腐败 - 老周横眉--92319ced.md',
        'thought-politics',
        '老周横眉/泰山围上135公里刀片铁丝网-祖国大好河山,是能让你免费看的吗- - 中国旅游乱象 - 中共腐败 - 老周横眉--92319ced.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/美联储变天-川普千挑万选的新主席,为何还是不肯降息- - 沃什 - 议息会议 - 降息 - 加息 - 格林斯潘 - 美国经济 - 中国经济 - 老周横眉--c793dbc7.md',
        '/home/ubuntu/思想与政治/老周横眉/美联储变天-川普千挑万选的新主席,为何还是不肯降息- - 沃什 - 议息会议 - 降息 - 加息 - 格林斯潘 - 美国经济 - 中国经济 - 老周横眉--c793dbc7.md',
        'thought-politics',
        '老周横眉/美联储变天-川普千挑万选的新主席,为何还是不肯降息- - 沃什 - 议息会议 - 降息 - 加息 - 格林斯潘 - 美国经济 - 中国经济 - 老周横眉--c793dbc7.md'
    ),
    (
        '/home/ubuntu/services/rag-app/data/video-transcripts/财富心理学-真正让你变富的,不是金融知识,而是你对金钱的心态和行为模式 - 老周横眉--87de0006.md',
        '/home/ubuntu/思想与政治/老周横眉/财富心理学-真正让你变富的,不是金融知识,而是你对金钱的心态和行为模式 - 老周横眉--87de0006.md',
        'thought-politics',
        '老周横眉/财富心理学-真正让你变富的,不是金融知识,而是你对金钱的心态和行为模式 - 老周横眉--87de0006.md'
    );

DO $$
BEGIN
    IF (
        SELECT count(*)
        FROM topic_partition_map AS mapping
        JOIN rag_documents AS source
          ON source.source_path = mapping.old_source_path
    ) <> 16 THEN
        RAISE EXCEPTION 'topic partition source inventory validation failed';
    END IF;
END
$$;

INSERT INTO rag_documents (
    source_path,
    knowledge_base,
    source_relative_path,
    source_name,
    source_type,
    source_sha256,
    embedding_model,
    embedding_dimensions,
    chunking_version,
    index_version,
    indexed_at
)
SELECT
    mapping.new_source_path,
    mapping.knowledge_base,
    mapping.source_relative_path,
    source.source_name,
    source.source_type,
    source.source_sha256,
    source.embedding_model,
    source.embedding_dimensions,
    source.chunking_version,
    2,
    now()
FROM topic_partition_map AS mapping
JOIN rag_documents AS source
  ON source.source_path = mapping.old_source_path
ON CONFLICT (source_path) DO UPDATE
SET knowledge_base = EXCLUDED.knowledge_base,
    source_relative_path = EXCLUDED.source_relative_path,
    source_name = EXCLUDED.source_name,
    source_type = EXCLUDED.source_type,
    source_sha256 = EXCLUDED.source_sha256,
    embedding_model = EXCLUDED.embedding_model,
    embedding_dimensions = EXCLUDED.embedding_dimensions,
    chunking_version = EXCLUDED.chunking_version,
    index_version = EXCLUDED.index_version,
    indexed_at = EXCLUDED.indexed_at;

INSERT INTO rag_chunks (
    document_id,
    chunk_index,
    content,
    character_count,
    embedding,
    created_at
)
SELECT
    target.id,
    chunk.chunk_index,
    chunk.content,
    chunk.character_count,
    chunk.embedding,
    now()
FROM topic_partition_map AS mapping
JOIN rag_documents AS source
  ON source.source_path = mapping.old_source_path
JOIN rag_documents AS target
  ON target.source_path = mapping.new_source_path
JOIN rag_chunks AS chunk
  ON chunk.document_id = source.id
ON CONFLICT (document_id, chunk_index) DO UPDATE
SET content = EXCLUDED.content,
    character_count = EXCLUDED.character_count,
    embedding = EXCLUDED.embedding,
    created_at = EXCLUDED.created_at;

COMMIT;
