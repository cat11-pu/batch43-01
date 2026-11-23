# GSB Bugfix Task
## 题面
域信任改造之后，安全审计用伪造的权限属性证书打进了资源域，拿到了不属于本域的管理员组，问题直接升级到集团。域控日志里只能看到登录成功，看不到证书里到底带了哪些组，审计追问时拿不出证据；资源域和账户域分属两个团队，任何改动都要双方窗口。校验器只验密钥分发中心签名不验服务签名；缓冲区偏移重叠也能解析；用户主体信息的长度字段越界后还继续往后读；跨林信任没做安全标识过滤，标识历史把对方域管理员带了进来；登录名时间戳不检查；签名失败后仍用证书里的组信息建令牌；同类型缓冲区出现两份时取第一个；缓冲区对齐填充被当成数据；用户改组后旧票据续期仍带旧证书；密钥分发中心签名用错密钥版本也能通过；空证书被当成无组用户直接放行；解析线程和缓存线程同时改同一个证书对象，读到一半被换掉。这个组件挂在所有域登录路径上，不能停服重来，域控那边还要求新旧两个林同时在线。签名不合法必须抛 PAC_SIGNATURE_INVALID 并拒绝登录，不能回退到未验证的组；来自不可信林的安全标识历史必须过滤，过滤后仍含对方域管理员标识时要报 PAC_SID_FILTERED；缓冲区偏移重叠必须按 PAC_BUFFER_OVERLAP 拒绝；证书年龄超过策略必须强制重新验证，不能静默沿用。集团安全部只给一周时间，登录链路一秒都不能断。

## 运行方式
```bash
python -m pytest tests -q
```

## 修复说明（PAC 校验器 fail-closed 改造）

`pac_claim_verifier.py` 在 `verify()` 内按下述顺序短路拒绝，任何拒绝都返回
`authenticated=False`、`groups=[]`，绝不回退到证书里未验证的组；全部通过后才建令牌。

| 漏洞 | 处置 |
| --- | --- |
| 只验 KDC 签名、不验服务签名 | 双签名都必须有效，任一失败抛 `PAC_SIGNATURE_INVALID`；`require_service_sig=False` 也无法关闭服务签名校验 |
| 签名失败仍用证书组建令牌 | 拒绝路径统一返回空组，令牌构建只消费已验证结果 |
| 缓冲区偏移重叠 | 声明区间相交即 `PAC_BUFFER_OVERLAP`；区间之间的空隙只当对齐填充，不当数据 |
| 长度字段越界仍继续读 | `offset/length` 为负或超出 `data` 即 `PAC_BUFFER_OVERFLOW` |
| 同类型缓冲区取第一份 | 重复类型直接 `PAC_DUPLICATE_BUFFER` |
| 不可信林 SID 历史带入管理员 | 不可信林 SID 一律过滤；过滤项含特权 RID（512/518/519/544）时抛 `PAC_SID_FILTERED` |
| 登录名时间戳不检查 | `logon_time` 晚于当前时间抛 `PAC_TIME_INVALID` |
| 旧票据续期沿用旧证书 | 证书年龄超过 `max_age` 抛 `PAC_REVALIDATE_REQUIRED`，强制向 KDC 重新验证 |
| KDC 签名密钥版本错误 | `key_version` 不在 `accepted_key_versions` 内抛 `PAC_KEY_VERSION` |
| 空证书当无组用户放行 | `empty=True` 或无有效内容抛 `PAC_EMPTY` |
| 签发林不可信 | `issuer` 不在 `trusted_forests` 内抛 `PAC_ISSUER_UNTRUSTED` |
| 解析/缓存线程并发改同一对象 | `verify()` 先深拷贝快照再校验，拷贝期间被改写则重试，持续失败即拒绝；审计日志加锁 |

新旧两个林同时在线：把两个林根域都放进 `trusted_forests` 即可，无需停服切换。
每次判定都会写入带锁审计环（`audit_trail()`），保留证书实际携带组、被过滤 SID
与决策码，供安全部追溯。

决策码常量统一以模块级字符串暴露（`PAC_SIGNATURE_INVALID`、`PAC_SID_FILTERED`、
`PAC_BUFFER_OVERLAP` 等），域控侧可直接按码做监控告警。

## 平台字段

所属领域：安全
子方向：身份认证
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
