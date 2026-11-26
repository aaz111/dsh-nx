# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循语义化版本。

## [1.0.10] - 2026-11-26

首个以 `@aaz111/dsh-nx` 发布到 npm 的版本。npm 上的 `dsh-nx` 是其他作者的包，与本项目无关。

### 修复

- **装配引用污染（严重）**：`nx_batch_drawings` 对装配组件执行 `SaveAs` 时，会把装配里该组件的引用改指到输出目录的副本，属于静默改坏用户装配。现在 `SaveAs` 后自动关闭副本并重新打开原路径，组件引用恢复原样。
  实测对 14 组件装配跑 11 个零件批量出图，出图后 BOM 路径 11/11 保持原路径。
- **`outDir` 指向源目录时会删掉源文件**：`SaveAs` 前无条件 `os.remove(model_target)`，当 `outDir` 恰好是零件所在目录时，`model_target` 就是源文件本身，会先删模型再写图纸。现在检测同路径并报错，不删文件。

### 改进

- **空闲 CPU 占用**：worker 主循环从固定 4 ms 忙等改为指数退避（4 ms → 100 ms），client 侧轮询同样退避。实测空闲 20 秒 CPU 增量从打满单核降到 1.36 s（6.8% 单核）。
- **优雅退出**：`stop` 从固定等 300 ms 就 `taskkill /F` 改为轮询等待应答，宽限 5 s，避免砍掉正在跑的导出任务。
- **同源校验**：`panel` 与 `preview` 路由新增 `sameOrigin()` 检查 —— 非 loopback 的 `Host` 拒绝（防 DNS rebinding），非 loopback 的 `Origin`/`Referer` 拒绝（防跨站）。16 项运行时验证通过，预览 iframe 与面板按钮功能零破坏。

### 文档

- 统一四处文档矛盾：Motion（实为已注册 5 个工具）、原生尺寸标注（`nx_dimension` 可用，`nx_annotate` 才依赖 PMI）、外螺纹（参数已接但本安装缺 `ThreadedShaft` 数据、提交必失败）、装配体导出（STL/IGES 可用，STEP 需先保存）。
- 两份 README 同步，功能表补充 Motion 一行。
- 包名变更为 `@aaz111/dsh-nx`，`cordis.patch.yml` 同步更新。

### 已知限制

- 同心圆 Ø 引线在圆密集时（如阀体俯视图 14 个圆）会交叉，需人工调整。
- 外螺纹参数可用但提交失败，属 NX 精简安装缺数据。
- 局部放大图内容空白、投影视图无隐藏线，均为 batch 会话限制。

## [0.5.1] - 2026-11-15

- 制图规则、批处理中可编辑 prt、面板尺寸拆分。

## [0.5.3]

原生尺寸标注（`nx_dimension`）打通 UF_DRF 路径；视图只留最少数量；圆柱长度标注。

## [0.5.0]

装配图、明细表、批量出图。
