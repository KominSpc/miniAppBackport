-- P1 阶段 MySQL 表结构骨架（模拟期不执行）
-- 约定：所有时间列存 UTC，读取后由服务端转换为 Asia/Shanghai 再输出。

CREATE TABLE IF NOT EXISTS anon_user (
  user_id       VARCHAR(40)  NOT NULL PRIMARY KEY,
  install_id    VARCHAR(64)  NULL,
  platform      VARCHAR(16)  NULL,
  app_version   VARCHAR(32)  NULL,
  access_token  VARCHAR(96)  NOT NULL,
  expires_at    DATETIME     NOT NULL,
  created_at    DATETIME     NOT NULL,
  preferences   JSON         NOT NULL,
  UNIQUE KEY uk_install (install_id),
  UNIQUE KEY uk_token (access_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS favorite (
  user_id      VARCHAR(40) NOT NULL,
  content_id   VARCHAR(40) NOT NULL,
  created_at   DATETIME    NOT NULL,
  PRIMARY KEY (user_id, content_id),
  KEY idx_content (content_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS history_entry (
  entry_id    VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id     VARCHAR(40) NOT NULL,
  kind        VARCHAR(16) NOT NULL,
  query_text  VARCHAR(128) NULL,
  content_id  VARCHAR(40)  NULL,
  created_at  DATETIME     NOT NULL,
  KEY idx_user_kind (user_id, kind, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS conversation (
  conversation_id VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id         VARCHAR(40) NOT NULL,
  title           VARCHAR(96) NULL,
  created_at      DATETIME    NOT NULL,
  updated_at      DATETIME    NOT NULL,
  KEY idx_user (user_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_message (
  message_id      VARCHAR(40) NOT NULL PRIMARY KEY,
  conversation_id VARCHAR(40) NOT NULL,
  role            VARCHAR(16) NOT NULL,
  content         TEXT        NOT NULL,
  expert          VARCHAR(16) NULL,
  live2d_action   VARCHAR(16) NULL,
  suggestions     JSON        NULL,
  created_at      DATETIME    NOT NULL,
  KEY idx_conversation (conversation_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
