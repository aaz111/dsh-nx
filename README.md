[README.md](https://github.com/user-attachments/files/34058034/README.md)
# dsh-nx

**Siemens NX 插件 · DeepSeek Harness 侧栏工作台**

在后台 NX 会话中完成三维建模、工程图纸、格式导出、装配管理与有限元分析，全部通过工具调用或侧栏面板驱动，无需手动操作 NX 图形界面。
甚至可以用许愿式的要求建模，但是ai生成不保证完全准确，生成结果需人工复核。

---

## 一、能力边界

### ✅ 能做什么

| 类别 | 工具 | 说明 |
|---|---|---|
| **会话管理** | `nx_status` `nx_session` `nx_open` `nx_new` `nx_info` `nx_save` | 查看/启动/重启 NX 会话，打开 .prt/.step/.iges，新建零件，查看信息，保存 |
| **测量** | `nx_measure` `nx_faces` | 体积、面积、质量、包围盒、质心；面列表（面积 + 中心点） |
| **实体建模** | `nx_primitive` `nx_extrude` `nx_revolve` `nx_polygon` | 长方体/圆柱/圆锥/球体，拉伸轮廓，旋转轮廓 |
| **曲线曲面** | `nx_curve` `nx_surface` | 直线/圆弧/圆/样条曲线；通过截面放样、网格曲面、拉伸曲面 |
| **基准与草图** | `nx_datum` `nx_sketch` | 基准平面/轴/点；矩形/圆/直线/多段线草图（自动约束），可直接拉伸/切除 |
| **布尔与细化** | `nx_boolean` `nx_fillet` `nx_chamfer` `nx_hole` `nx_thread` `nx_shell` | 合并/切除/相交，圆角，倒角，真实 NX 孔特征，内螺纹（符号+详细），抽壳 |
| **变换与阵列** | `nx_transform` `nx_pattern` `nx_expression` | 移动/旋转/镜像，线性/环形阵列，读写参数表达式 |
| **工程图纸** | `nx_drawing` `nx_pdf` `nx_annotate` | （能用，但不建议用，只能帮你出三视图但是作为人类不一定能看懂）自动投影视图 + 图纸，导出 PDF，自动标注（已有 PMI） |
| **制图补充** | `nx_section_view` `nx_detail_view` `nx_note` | 剖视图（指定剖切线），局部放大图（圆形区域 + 比例），文字注释（自动选中文字体） |
| **BOM 与批量出图** | `nx_bom_table` `nx_batch_drawings` | 在图纸上画明细表（自绘表格线 + 每格注释），逐件 A3 图纸 + PDF |
| **格式导出** | `nx_export` | STEP / IGES / STL（等待 NX 异步翻译器完成） |
| **装配** | `nx_assembly_add/move/fix/mate/list/gap` | 插入组件、定位、约束、最小间隙检查 |
| **组件操作** | `nx_component_pattern` `nx_component_op` `nx_explode` | 组件阵列、替换/抑制/删除、爆炸图（真实位移，可还原） |
| **装配清单** | `nx_assembly_bom` `nx_assembly_clearance` | 按零件号汇总 BOM（数量/体积/质量，可导 CSV），逐对间隙矩阵（标记干涉） |
| **图层与引用集** | `nx_layers` `nx_refset` | 图层状态管理，移动实体到图层；引用集列表/创建/删除 |
| **有限元** | `nx_fea_spec` `nx_fea_solve` `nx_fea_solid` | NX Nastran 悬臂梁快速校核（SOL 101）；真实几何 FE（体素/铺面网格，力/压力/力矩/重力/离心，静力/模态，挠度 + Von Mises 云图） |
| **底层逃生舱** | `nx_python` | 在 NX 会话内执行任意 NXOpen Python 代码 |

**单位统一**：mm / 度 / kg。

### ❌ 不能做什么（已知限制）

| 限制 | 原因 | 替代方案 |
|---|---|---|
| DXF / DWG 导出 | NX 批处理翻译器挂起 | 用 STEP / IGES / STL |
| 原生标注尺寸 | 批处理无法创建 PMI 标注 | 用 `nx_note` 手写文字注释 |
| 草图约束尺寸 | NXOpen `CreateDimension` 未打通 | 用 `nx_expression` 驱动参数 |
| 隐藏线 | 批处理无隐藏线消除 | 用 `nx_section_view` 剖视展示内部 |
| 局部放大图内容为空 | 批处理无缩放渲染 | 用剖视图替代 |
| 图框 / 标题栏 | 模板图框不随 PDF 输出 | 用 `nx_note` 补标题栏文字 |
| 外螺纹 | NX 本机外螺纹数据缺失 | 仅支持内螺纹 |
| 直纹面 / 扫掠 / 投影 / 相交 / 回转曲面 | NXOpen 提交即报错 | 用 `through_curves` / `mesh` / `extrude` 替代 |
| 装配体直接导 STL | 装配体无自身 Body | 用面板预览或导出单个零件 |
| `nx_fillet` 重叠圆角 | NX 拒绝重叠倒角 | 分批选边圆角 |
| NX Motion / 仿真运动 | 未暴露 | — |
| 装配求解器自动移动组件 | NX 批处理仅注册约束不求解 | 用 `nx_assembly_move` 手动定位 |
| 真正共形网格 | 需 Simcenter 网格器 | 用 `draped` 网格（体积误差 −0.9%，孔壁仍为体素形状） |
| Nastran 默认内存 14 GB | 桌面机不足 | 已改用 `mem=2000mb` |

---

## 二、使用说明

### 2.1 安装

```bash
# 方式一：从 Release 下载 zip 解压
# 方式二：从源码安装
pnpm add file:<解压目录>/dsh-nx
```

**前提**：本地已安装 Siemens NX。安装路径由 `nxHome` 配置项或 `UGII_BASE_DIR` 环境变量指定；两者都不给时自动探测常见安装位置中带 `NXBIN\run_journal.exe` 的目录。FE 工具额外需要 `simulation` / `nastran` 许可证。

### 2.2 命令行工具

直接对 AI 说出需求即可，插件会自动选择工具。也可以显式调用：

```
nx_status          # 查看 NX 会话状态
nx_open            # 打开零件文件
nx_new             # 新建 mm 零件
nx_measure         # 测量体积/面积/质量/包围盒
nx_primitive       # 创建基本体
nx_extrude         # 拉伸轮廓
nx_boolean         # 布尔运算
nx_export          # 导出 STEP/IGES/STL
nx_drawing         # 创建图纸（自动投影视图）
nx_pdf             # 导出 PDF
nx_section_view    # 创建剖视图
nx_detail_view     # 创建局部放大图
nx_note            # 添加文字注释
nx_bom_table       # 在图纸上画明细表
nx_batch_drawings  # 批量出图（逐件 A3 + PDF）
nx_fea_solid       # 真实几何有限元分析
nx_python          # 执行 NXOpen Python 代码
```

### 2.3 侧栏面板

在 DeepSeek Harness 侧栏点击 **NX** 图标即可打开工作台面板：

- **会话卡**：NX 进程 ID、版本、已打开零件、运行时长
- **打开零件**：输入路径打开文件，支持中文路径（自动解码 URL 编码）
- **分组操作**：
  - 零件：信息 / 测量 / 面列表 / 图层 / 引用集
  - 出图：建图纸（可选 A4/A3/A2/A1）/ 打印 PDF / 剖视图 / 技术要求
  - 文件：STEP / IGES / STL / 保存
  - 装配：装配清单 / BOM 表（可选中英文 + 七列）/ BOM CSV / 间隙检查 / 爆炸图 / 批量出图
- **上一步结果**：显示最近操作的输出摘要 + 文件链接（点击可预览/打开文件夹）
- **最近产物**：输出目录下的最新文件列表，新文件标绿点
- **3D 预览**：点击预览按钮查看 STL 网格渲染（支持剖切滑杆）

### 2.4 配置项

| 配置键 | 默认值 | 说明 |
|---|---|---|
| `panel` | `true` | 设为 `false` 跳过侧栏面板路由 |
| `outputDir` | `~/.dsh-nx/out` | 产物收集目录（也可用 `DSH_NX_OUT` 环境变量指定）|
| `nxHome` | 自动探测 | NX 安装路径（也可用 `UGII_BASE_DIR` 环境变量指定）|

### 2.5 快速上手示例

```
# 1. 新建零件并建模
nx_new → 创建 120×80×20 长方体 → 打孔 → 圆角 → 测量

# 2. 出图纸
nx_drawing（A3）→ nx_section_view → nx_note → nx_pdf

# 3. 装配
nx_assembly_add → nx_assembly_move → nx_assembly_bom → nx_batch_drawings

# 4. 有限元
nx_fea_solid（mesh=draped, loads=pressure+force）→ 查看云图
```

---

## 三、免责声明

**AI生成警告！！！！！**不能保证完全准确，任何功能都是，所有输出均要人工复核才能下一步。

1. **本插件是第三方工具**，非 Siemens 官方产品。NXOpen API 的行为可能随 NX 版本更新而变化，本插件仅在 **NX 2512** 上测试通过，其他版本未经验证。

2. **模型数据安全**：本插件通过后台 NX 会话操作零件文件。请在使用前**备份原始文件**。插件不会主动保存文件（除非显式调用 `nx_save`），但异常中断可能导致未保存的更改丢失。

3. **有限元结果仅供参考**：`nx_fea_solid` 使用体素/铺面网格近似几何，结果精度受网格密度和边界条件简化影响。**不得直接用于工程设计决策或安全认证**，重要结构请使用 Simcenter / ANSYS 等专业 FE 软件复核。

4. **导出文件格式兼容性**：STEP / IGES 导出遵循 ISO 标准，但不同 CAD 软件对格式的解释可能存在差异。请在目标软件中验证导入结果。

5. **第三方依赖**：FE 工具依赖 NX Nastran 求解器（需 `simulation` / `nastran` 许可证）。无许可证时 FE 相关工具将报错，但不影响其他功能。

6. **使用风险**：本插件按 MIT 许可证提供，**不附带任何明示或暗示的担保**。使用本插件产生的任何直接或间接损失（包括但不限于数据丢失、模型损坏、生产延误），作者不承担任何责任。

7. **开源组件**：本插件使用了 LibreOffice Kit（文档渲染）、numpy、Pillow（云图绘制）等开源组件，各自遵循其原始许可证。

---

> **License**: MIT  
> **Author**: aaz111  
> **Repository**: [github.com/aaz111/dsh-nx](https://github.com/aaz111/dsh-nx)
