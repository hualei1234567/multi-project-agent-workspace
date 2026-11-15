"""测试公共设施。

**为什么需要这里**：CI（GitHub Actions）的 runner 配了环境代理
（`http_proxy`/`HTTP_PROXY` 等），`urllib.request.urlopen` 默认会读这些变量，
于是对 `127.0.0.1:随机端口` 的请求也被送去代理，返回 503 Service Unavailable。

本地开发机通常没有代理变量，所以这个问题**本地测不出来**——首次 CI 运行才暴露。
修复方式：显式构造一个不用代理的 opener，所有测试对本地实例的请求都走它。
"""

from __future__ import annotations

import urllib.request
from typing import Any

#: 不走任何代理的 opener。对回环地址发请求时用它，绕开环境里的 http_proxy。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_urlopen(request: Any, timeout: float = 10) -> Any:
    """对本地 HTTP 服务发请求，绕过环境代理。

    用法与 ``urllib.request.urlopen`` 一致，但忽略 ``http_proxy`` 等环境变量。
    测试里的服务器都监听 127.0.0.1，不应该经过任何代理。
    """
    return _NO_PROXY_OPENER.open(request, timeout=timeout)
