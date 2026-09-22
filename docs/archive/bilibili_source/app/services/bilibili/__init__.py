"""B 站适配器（真实视频内容源）。

模块划分：
- ``client``：上游 HTTP 客户端（公开接口，不需要登录态）；
- ``adapter``：B 站载荷 → 本服务 ContentItem 的映射；
- ``source``：数据源装配与进程级缓存（``VIDEO_SOURCE=bilibili`` 时接管取数）。
"""