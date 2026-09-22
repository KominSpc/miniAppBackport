-- 匿名用户与互动数据的表结构（模拟期由内存仓储承担，设置 DATABASE_URL 后切到这里）。
--
-- 约定：
-- 1. 所有时间列一律存 **UTC**（DATETIME，无时区），读取时在应用层转回 Asia/Shanghai。
--    绝不使用 NOW() 写入 —— 服务器的 system 时区不一定是 UTC（实测线上那台就是北京时间），
--    用 NOW() 会静默偏移 8 小时。
-- 2. 表名一律小写：Linux 上 lower_case_table_names=0，表名大小写敏感。
-- 3. seq 是插入顺序代理键。时间列只到秒（与契约的 now() 一致），
--    同一秒内的多条记录靠 seq 才能稳定排序。
-- 4. 兼容 MySQL 5.7（线上是 5.7.44）：不用 8.0 专有语法，JSON 列不给默认值。

CREATE TABLE IF NOT EXISTS anon_user (
  user_id      VARCHAR(40)  NOT NULL PRIMARY KEY,
  install_id   VARCHAR(64)  NULL,
  platform     VARCHAR(16)  NULL,
  app_version  VARCHAR(32)  NULL,
  access_token VARCHAR(96)  NOT NULL,
  expires_at   DATETIME     NOT NULL,
  created_at   DATETIME     NOT NULL,
  preferences  JSON         NOT NULL,
  UNIQUE KEY uk_install (install_id),
  UNIQUE KEY uk_token (access_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 图片 / 内容收藏（幂等）。favorite_count 由 COUNT(*) 现算，不再单独维护计数列。
CREATE TABLE IF NOT EXISTS favorite (
  seq        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id    VARCHAR(40) NOT NULL,
  content_id VARCHAR(64) NOT NULL,
  created_at DATETIME    NOT NULL,
  UNIQUE KEY uk_user_content (user_id, content_id),
  KEY idx_content (content_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 收藏那一刻的条目快照：收藏列表因此不必逐条回源上游（见 app/api/v1/images.py）。
CREATE TABLE IF NOT EXISTS favorite_snapshot (
  seq        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id    VARCHAR(40) NOT NULL,
  content_id VARCHAR(64) NOT NULL,
  payload    JSON        NOT NULL,
  created_at DATETIME    NOT NULL,
  UNIQUE KEY uk_user_content (user_id, content_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 浏览 / 搜索历史。整条记录存 payload，另把可筛选的字段提到列上。
CREATE TABLE IF NOT EXISTS history_entry (
  seq        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  entry_id   VARCHAR(40)  NOT NULL,
  user_id    VARCHAR(40)  NOT NULL,
  kind       VARCHAR(16)  NOT NULL,
  query_text VARCHAR(191) NULL,
  payload    JSON         NOT NULL,
  created_at DATETIME     NOT NULL,
  UNIQUE KEY uk_entry (entry_id),
  KEY idx_user_kind (user_id, kind, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 音乐歌单（收藏夹）。default_flag=1 表示默认歌单；普通歌单为 NULL。
-- UNIQUE(user_id, default_flag) 靠「NULL 互不相等」放行任意多个普通歌单，
-- 同时保证一个用户最多只有一个默认歌单（不会并发建出两个）。
CREATE TABLE IF NOT EXISTS music_playlist (
  seq          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  playlist_id  VARCHAR(40) NOT NULL,
  user_id      VARCHAR(40) NOT NULL,
  name         VARCHAR(96) NOT NULL,
  default_flag TINYINT     NULL,
  created_at   DATETIME    NOT NULL,
  UNIQUE KEY uk_playlist (playlist_id),
  UNIQUE KEY uk_user_default (user_id, default_flag),
  KEY idx_user (user_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 歌单内的曲目（加入那一刻的曲目快照）。seq 即「加入先后」。
CREATE TABLE IF NOT EXISTS music_playlist_item (
  seq         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  playlist_id VARCHAR(40) NOT NULL,
  track_id    VARCHAR(64) NOT NULL,
  payload     JSON        NOT NULL,
  created_at  DATETIME    NOT NULL,
  UNIQUE KEY uk_playlist_track (playlist_id, track_id),
  KEY idx_track (track_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 宠物对话。
CREATE TABLE IF NOT EXISTS conversation (
  seq             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  conversation_id VARCHAR(40) NOT NULL,
  user_id         VARCHAR(40) NOT NULL,
  title           VARCHAR(96) NULL,
  created_at      DATETIME    NOT NULL,
  updated_at      DATETIME    NOT NULL,
  UNIQUE KEY uk_conversation (conversation_id),
  KEY idx_user (user_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS chat_message (
  seq             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  message_id      VARCHAR(40) NOT NULL,
  conversation_id VARCHAR(40) NOT NULL,
  user_id         VARCHAR(40) NOT NULL,
  role            VARCHAR(16) NOT NULL,
  content         TEXT        NOT NULL,
  expert          VARCHAR(16) NULL,
  live2d_action   VARCHAR(16) NULL,
  suggestions     JSON        NOT NULL,
  created_at      DATETIME    NOT NULL,
  UNIQUE KEY uk_message (message_id),
  KEY idx_conversation (conversation_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 宠物的隐藏好感度：每个用户一条，随对话累加（见 app/services/pet.py）。
-- 界面上不显示，只体现在模型说话的口气里；放这里是为了跨重启不丢
-- ——「换台设备还认得你」正是好感度该有的样子。
CREATE TABLE IF NOT EXISTS pet_affection (
  seq        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id    VARCHAR(40) NOT NULL,
  score      INT         NOT NULL,
  updated_at DATETIME    NOT NULL,
  UNIQUE KEY uk_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
