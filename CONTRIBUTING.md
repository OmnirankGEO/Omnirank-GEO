# 参与贡献 / Contributing

感谢你愿意为 OmniRank 出力。提交之前,请读完这一页。

## 1. 贡献按同一许可证进来

你提交的代码、文档和其他内容,按本项目的同一许可证授权:**Apache License 2.0 + [LICENSE](LICENSE) 开头的署名附加条件**(inbound = outbound)。我们不要求另签贡献者协议(CLA)。

Contributions are licensed under the same terms as the project: the Apache License 2.0 together with the Additional Condition at the top of [LICENSE](LICENSE) (inbound = outbound). No separate CLA is required.

## 2. 每个 commit 都要签 DCO

我们用 [Developer Certificate of Origin 1.1](https://developercertificate.org/)(DCO)确认贡献来源。每个 commit 的提交信息末尾都要有一行:

```
Signed-off-by: 你的名字 <你的邮箱>
```

名字和邮箱要与 commit 作者一致。用 `git commit -s` 会自动加上这一行。忘了加,可以用 `git commit --amend -s`(最近一个 commit)或 `git rebase --signoff <起点>`(多个 commit)补上,再强推到你自己的分支。

没有 `Signed-off-by:` 的 pull request 不会被合并。

加上这一行,表示你认可下面的 DCO 1.1 原文:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## 3. 提交前

- 一个 pull request 只做一件事,说明改了什么、为什么改。
- 改了行为的地方,请附上能复现的测试或验证步骤。
- 不要提交密钥、口令、证书、真实客户数据或任何 `.env` 文件。
- 保留界面、报告和客户页面上的 OmniRank 署名与项目主页链接 —— 这是许可证的附加条件,删除它的改动不会被合并。
