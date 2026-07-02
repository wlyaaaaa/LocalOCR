# LocalOCR 测试报告
日期：2026-07-01 17:39

## GPU 环境

- GPU: NVIDIA GeForce RTX 5080 | capability=sm_120 | count=1 | place=gpu:0 | op_ok=True
- 推理前显存：5117 MiB

## 中文截图
- 文件：`sample_chat_screenshot.png`
- 引擎：`ocr`
- 模型：`PP-OCRv6_medium (det + rec)`
- 耗时：2.47s
- 显存：5117 MiB → 5738 MiB
- 页数：1，块数：9
- 输出：`sample_chat_screenshot.md` / `sample_chat_screenshot.json`
- 方向角度：0

### 识别文本片段

```
你好，关于上周那份合同有几个问题想确认
第三条的违约金比例是否可以再谈
好的，违约金我建议按日万分之五计算
交货期可以延到8月31日
那付款方式呢，能不能分三期
以，签约付 30%，到货付 50%，验收后付 20%
好的，我让法务按这个改一版
麻烦尽快，我们这周要定下来
```

## 扫描PDF
- 文件：`sample_scan.pdf`
- 引擎：`vl`
- 模型：`PaddleOCR-VL-1.6`
- 耗时：11.87s
- 显存：5739 MiB → 14710 MiB
- 页数：2，块数：17
- 输出：`sample_scan.md` / `sample_scan.json`
- 方向角度：None

### 识别文本片段

```
中华人民共和国民法典合同编相关条款节选
第四百六十五条 依法成立的合同，受法律保护。
依法成立的合同，仅对当事人具有法律约束力，但是法
第四百七十条 合同的内容由当事人约定，一般包括下列
（一）当事人的姓名或者名称和住所；
（二）标的；
（三）数量；
（四）质量；
第五百条 当事人在订立合同过程中有下列情形之一，造成对方损失的，应当承担赔偿责任：
（一）假借订立合同，恶意进行磋商；
```

## 表格
- 文件：`sample_table.png`
- 引擎：`vl`
- 模型：`PaddleOCR-VL-1.6`
- 耗时：6.41s
- 显存：14710 MiB → 15295 MiB
- 页数：1，块数：3
- 输出：`sample_table.md` / `sample_table.json`
- 方向角度：0

### 识别文本片段

```
采购订单明细表 PO-2026-0712
<table><tr><td>序号</td><td>商品名称</td><td>规格型号</td><td>数量</td><td>单价(元)</td><td>金额(元)</td></tr><tr><td>1</td><td>工业级路由器</td><td>RJ-4500</td><td>20</td><td>1,280.00</td><td>25,600.00</td></tr><tr><td>2</td><td>光纤收发器</td><td>GF-1000S</td><td>50</td><td>320.00</td><td>16,000.00</td></tr><tr><td>3</td><td>网络交换机</td><td>SW-2400</td><td>8</td><td>4,500.00</td><td>36,000.00</td></tr><tr><td>4</td><td>服务器机柜</td><td>CAB-42U</td><td>3</td><td>6,800.00</td><td>20,400.00</td></tr><tr><td>5</td><td>不间断电源</td><td>UPS-3000</td><td>5</td><td>2,900.00</td><td>14,500.00</td></tr></table>
合计金额：112,500.00元
```

## 公式文档
- 文件：`sample_formula.png`
- 引擎：`vl`
- 模型：`PaddleOCR-VL-1.6`
- 耗时：126.49s
- 显存：15295 MiB → 15846 MiB
- 页数：1，块数：13
- 输出：`sample_formula.md` / `sample_formula.json`
- 方向角度：0

### 识别文本片段

```
机器学习常用损失函数
一、均方误差（MSE）用于回归任务：
$$ \mathsf{MSE}=(\mathbf{1/n})^{*}\Sigma(\mathbf{yi}-\mathbf{i})^{2} $$
其中 yi 为真实值，⊠i 为预测值，n 为样本数。
二、交叉熵损失用于分类任务：
$$ {\mathsf{L}}=-\Sigma{\mathrm{~c i~}}^{*}\log(p i) $$
其中 ci 为真实分布，pi 为预测概率分布。
三、正则化项 L2 范数：
```

## 汇总

| 样本 | 结果 | 耗时 |
|---|---|---|
| 中文截图 | ✓ | 2.5s |
| 扫描PDF | ✓ | 11.9s |
| 表格 | ✓ | 6.4s |
| 公式文档 | ✓ | 126.5s |

推理后显存：15846 MiB
