# Lumina Pendant Prep

Windows 和 macOS 桌面图像预处理工具，面向 Lumina-Layers 等多层叠色 FDM 工作流。当前版本提供可运行的图形界面原型，可视化净化色块、编辑挂坠裁切并导出透明 PNG。

## 目标

将常见照片格式（包括 HEIC/HEIF）转换为容易检查、方便继续导入 Lumina 的 PNG。用户可以预览并控制小色块合并，也可以为挂坠设置裁切轮廓、圆角和透明挂绳孔。

## 当前原型

- 图形界面支持打开/拖放照片、参数调节、后台预览、撤销/重做和保存/重开 `.lpproj` 项目。
- 输入 JPEG、PNG、WebP、BMP、TIFF、HEIC/HEIF 和 AVIF；读取 EXIF 方向，尝试转换嵌入的 ICC 配置至 sRGB。
- 无抖动调色、相邻近似色合并、小连通色岛合并，并高亮净化影响区域；喷嘴尺寸作为最小特征阈值下限，同时报告可能过窄的连通区域。
- 通过拖动裁切框、选择矩形/圆形/圆角矩形或点击绘制自定义多边形创建挂坠轮廓。
- 可设置毫米圆角、挂绳孔直径和孔位置；画布外区域和孔内均为 PNG alpha 透明像素。
- 导出 RGBA PNG，PNG DPI 按目标物理宽度写入。

算法输出是打印可行性估计。真实照片和材料组合仍应先打印小样确认；复杂照片中的窄线不会被默认删掉，而会给出提示。

## 运行

需要 Python 3.10 或更新版本。Windows/macOS 本地开发环境均可执行：

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python run_app.py
```

macOS：

```bash
source .venv/bin/activate
python -m pip install -e .
python run_app.py
```

GitHub Actions 在 Windows x64、macOS Apple Silicon 和 macOS Intel runner 上分别生成未签名的打包产物。macOS 用户需在首次运行时手动确认打开；代码签名/公证和最终用户安装体验仍需后续完善。

## 许可证状态

项目代码的许可证尚未选定；在仓库添加正式许可证之前，公开可见不等于授予代码再分发或修改权限。第三方依赖仍受其各自许可证约束。

## 操作

1. 打开照片，设置目标宽度、喷嘴、最小特征宽度和净化参数。
2. 在左侧图像拖动鼠标定义裁切框；选择“点击设置孔位置”后在图像上定位孔。
3. 选择轮廓类型。绘制多边形时左键添加节点、右键结束；重新开始时按“重置裁切框”。
4. 勾选预览区“标记已简化区域”查看受影响的像素。
5. 保存项目以保留原照片路径和参数；导出透明 PNG 后导入 Lumina-Layers。

## 技术路线

- Python + PySide6：一个桌面应用代码库，使用 Qt 提供 Windows/macOS 图形界面。
- Pillow：常见格式解码、EXIF 方向、RGBA/PNG、裁切、遮罩与圆角。
- pillow-heif：HEIC/HEIF 解码并注册为 Pillow 插件；对应用安装包做真实照片兼容验证。
- NumPy + OpenCV：颜色标签、连通域、小色岛筛选、距离/边缘约束等计算。通过独立处理模块隔离算法，GUI 不直接操作像素循环。
- PyInstaller 或 Nuitka：分别在 Windows 与 macOS 构建和签名/打包；本地依赖、HEIF codec 和安装包大小需在技术验证阶段确认。

最终选型需先做最小技术验证，特别检查 HEIC 在 Windows/macOS 打包后的解码表现、Apple Silicon 支持、色彩配置、运行速度和分发许可。

## 交互流程草案

1. 打开或拖入照片，显示原图及文件信息；自动应用 EXIF 旋转。
2. 设置目标宽度、喷嘴直径与最小特征宽度，实时生成净化预览。
3. 用并排/叠加视图比较原图和结果；显示色岛合并图层，允许调参数并撤销。
4. 选择挂坠轮廓并编辑边界；设置圆角和挂绳孔的直径、位置。
5. 检查 alpha 透明区域及打印尺寸，导出 PNG 和可选项目 JSON。

默认操作均非破坏性。用户确认参数后再输出文件；不自动替用户提交切片或打印。

## 实施阶段

1. **技术验证**：PySide6 跨平台窗口、HEIC/HEIF 解码、RGBA PNG 导出与安装包依赖验证。
2. **图像核心**：方向/色彩处理、毫米到像素换算、基于感知色差和连通域的小色块合并；用合成图像和实际打印照片建立案例。
3. **桌面 MVP**：导入、对比预览、参数控制、撤销/重做、圆/圆角矩形/多边形裁切、透明孔、PNG 导出。
4. **可用性与分发**：项目设置保存、边缘情况提示、Windows/macOS 安装包与用户文档。
5. **打印验证迭代**：用不同尺寸、喷嘴、材料配色的实物打印检查边界；据此调整默认阈值。

## 开源复用与参考

采用成熟库承担通用编解码、图形界面与像素运算；自研部分聚焦“按实体打印尺寸治理色块”及挂坠流程的交互。

- [Lumina-Layers](https://github.com/lumina-layer-studio/Lumina-Layers)：目标下游；已有校色、局部颜色处理与 3MF 导出，本项目输出可检查的 PNG，不重复实现打印配方与切片导出。
- [pillow-heif](https://github.com/bigcat88/pillow_heif)：Pillow 的 HEIF/HEIC 支持；优先验证其打包及 codec 依赖。
- [Pillow](https://github.com/python-pillow/Pillow)：格式读写、EXIF 方向、alpha 通道与遮罩基础操作。
- [OpenCV](https://github.com/opencv/opencv)：连通域标记、形态学操作等通用图像算子；只选取适合打印边界且容易回退的处理。
- [MultiClean](https://github.com/DPIRD-DMA/MultiClean)：多类别栅格的小岛移除与边缘清理思路参考。其遥感场景不直接复用为默认参数。
- [ImageLayerDecomposer](https://github.com/CactusQuill/ImageLayerDecomposer)：颜色聚类、LAB 感知色差、连通域与预览交互的案例参考；关注其许可后再决定是否复用代码。
- 工作区已有 `make-photo-stamp-archive` 脚本：挂绳孔透明裁切、圆角/边框相关处理和颜色孤岛简化可作为需求与算法基线；先检查并抽取通用规则，避免复制后形成两套实现。

复用第三方实现前需逐项核对代码许可证、依赖许可证、维护状态和分发义务。当前计划不复制上述仓库代码。

## 方案边界与待验证项

- “相似颜色”须按感知色差判断，而不是仅按 RGB 欧氏距离；但应提供可理解的强度设置。
- 面积阈值之外还需考虑细长特征宽度；同面积的圆点与细线对打印的可行性不同。
- 对人脸、文字和主体轮廓的保护策略需通过照片样例验证，不能承诺自动识别后绝不误伤。
- HEIC 容器可能包含多帧、HDR 或特殊色彩信息；首版明确支持范围和失败提示。
- “挂坠裁切”首版定义为用户可控的几何轮廓裁切；自动抠出照片主体可作为后续独立功能评估。

## 参考资料

详细决策、界面草图和后续修订记录见 [`docs/PLAN.md`](docs/PLAN.md)。
