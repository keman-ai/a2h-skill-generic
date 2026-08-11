"""站点底座 —— **由 release/package.py 生成，不要手改**。

本产物的环境：prod。按业主定的原则，一个产物只含一套环境的域名；
改域名请改 release/site_profiles.py，那里是两套环境的唯一来源。
"""

ENV = "prod"
SITE = "https://a2hmarket.ai"
AUTH_API = "https://api.a2hmarket.ai/findu-user"
FRONT_BASE = "https://a2hmarket.ai"

# 退役域名就地改写（详见 release/site_profiles.py 的说明）。
RETIRED_SITES = {
}
