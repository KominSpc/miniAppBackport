"""Pixiv 适配器（真实内容源）。

模块划分：
- ``client``：上游 HTTP 客户端（唯一持有 Cookie 的地方）；
- ``adapter``：Pixiv 载荷 → 本服务 ContentItem 的映射；
- ``source``：数据源装配与进程级配置（``CONTENT_SOURCE=pixiv`` 时接管取数）。
"""