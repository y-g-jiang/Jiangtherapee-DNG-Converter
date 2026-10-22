# 添加一台新相机

下面这段提示词可以整段交给 AI（Claude / GPT / DeepSeek 等具备文件读写与命令执行能力的代理）。
它会引导代理完成"采集相机常量 → 写入记录 → 验证出图"的全过程。

**硬性要求：必须用真正的 Adobe DNG Converter 产出的 DNG 作为测量基准。** 没有基准就只能
猜，而猜出来的色彩矩阵会让所有照片偏色。仓库不接收没有基准数据的 PR。

---

## 提示词（复制以下全部内容给 AI）

````text
你在一个名为 "Jiangtherapee DNG Converter" 的项目里工作。这个项目是一个独立的
raw → DNG 转换器：它不调用 Adobe 的任何代码，而是依靠一份"相机记录"
（ref/camera_db.json）把 raw 解码后写成合法 DNG。目前记录里有 166 台相机，
你的任务是**新增一台相机**，并让转换结果通过验收。

======================
0. 先读懂数据结构
======================

目录布局：
  tools/convert.py            转换入口
  tools/build_dng.py          构建流水线（解码 → 取窗口 → 生成 spec → 写 DNG）
  tools/ingest_libraw.py      LibRaw 适配层，解码 raw 并返回像素与元数据
  tools/render_preview.py     本地预览渲染
  bin/dngbuild.exe            spec 驱动的 DNG 写入器（DNG SDK 1.7.1）
  bin/srhq_joraw2.dll         LibRaw 0.22.2 适配器
  ref/camera_db.json          相机记录（你要改的文件）
  ref/model_aliases.json      LibRaw 机型名 → 记录键的映射（你要改的文件）
  ref/xmp_props.json          XMP 常量（可选，按相机/模式分组）

ref/camera_db.json 的结构（顶层键是相机名，例如 "Canon EOS 5D Mark III"）：

  {
    "<Camera>": {
      "constants": {
        "ifd0/0x010F":   {"value": "Canon", "type": 2},
        "subifd/0xC61A": {"value": [[512,1],[512,1],[512,1],[512,1]], "type": 5},
        "subifd/0xC621": {"value": [[...],[...],...], "type": 10},
        ...
      },
      "window": {
        "default": [dx, dy, w, h]
      },
      "noise_iso":   {"default|100": [[...],[...]]},
      "neutral_iso": {"default|100": [[n,d],[n,d],[n,d]]},
      "preview_digest": {"default": {"value": [...], "type": 7}}
    }
  }

各字段含义（这是关键，务必按此理解）：

* constants：键是 "<ifd0|subifd|exif>/0xTAG"，value/type 直接对应 DNG 标签。
  必须包含的有：
    - ifd0/0x010F Make、ifd0/0x0110 Model
    - subifd/0xC612 DNGVersion、subifd/0xC614 UniqueCameraModel
    - subifd/0xC61A BlackLevel（二维/四元组，类型 5 即 RATIONAL）
    - subifd/0xC61D WhiteLevel
    - subifd/0xC61F DefaultCropOrigin、subifd/0xC620 DefaultCropSize
    - subifd/0xC68D ActiveArea
    - subifd/0xC621 ColorMatrix1、subifd/0xC622 ColorMatrix2
    - subifd/0xC65A CalibrationIlluminant1、subifd/0xC65B CalibrationIlluminant2
    - ifd0/0xC628 AsShotNeutral
    - subifd/0x828E CFAPattern、subifd/0xC616 CFAPlaneColor（单色机除外）
  建议一并采集（有就抄，没有就跳过，不要编造）：
    - 0xC62A BaselineExposure、0xC62B/C BaselineNoise/Sharpness
    - 0xC714/0xC715 ForwardMatrix1/2
    - 0xC6F6/0xC6F7 ProfileHueSatMap 数据、0xC6F8 ProfileName
    - 0xC6FA/0xC6FB/0xC6FC ProfileToneCurve 及表大小
    - 0xC71A/C719 预览相关、0xC6FE 版权串、0xC62F 机身序列号
    - 0xC634 DNGPrivateData（私有数据块，逐字节）
    - 0xC74E OpcodeList3（镜头校正；见第 4 节的强制规则）
* window：把 LibRaw 解出的整幅像素对齐到 ADC 输出的那一块。
  dx,dy = 相对 LibRaw 像素原点的偏移，w,h = ADC 输出 CFA 的宽高。
* noise_iso / neutral_iso：键是 "<模式>|<ISO>"，模式取文件名里 "__A__B__" 的 A 段，
  没有则为 "default"。neutral 是 AsShotNeutral 的三组有理数，noise 是噪声模型。
  这两项只影响"逐字节复刻"评分，出图必需的是 constants 与 window。

======================
1. 准备基准（必须有）
======================

需要一份真实的 Adobe DNG Converter（下称 ADC）。按顺序尝试：

1) 检测已安装：
   - Windows: 检查 "C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe"
   - macOS:  检查 /Applications/Adobe DNG Converter.app
   找到就直接用，不要下载。

2) 没找到就提示我下载：
   - 官方地址：https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html
   - 只从 adobe.com 下载；下载完把安装路径告诉脚本。
   - 不要尝试破解、绿色版或第三方镜像。

用 ADC 转换样张（命令行为准）：
   "Adobe DNG Converter.exe" -c -d <输出目录> <样张 raw>
   （-c = 压缩，-d = 输出目录；ADC 会打印 GPU/WinML 警告，那是无害的，忽略。）

同时需要同一张 raw 的 LibRaw 解码结果，用项目自带适配器：
   python -B tools/ingest_libraw.py --one --file <样张 raw>
   它会打印一段 JSON，含 raw_width/raw_height/width/height/top_margin/left_margin、
   black、maximum、cfa、cam_mul、cam_xyz 等。

======================
2. 采集数据（只测量，不推测）
======================

写一个一次性脚本（可放在 work/ 下，不要提交进仓库）完成：

a) 解析 ADC 产出的 DNG（用 tools/compare.py 里的 Tiff/values 即可：
   import sys; sys.path.insert(0, "tools"); from compare import Tiff, values）
   把第 0 节列出的标签逐个读出，写成 constants。

b) 求 window：
   - w,h = ADC DNG 中 CFA 子 IFD 的 ImageWidth/ImageLength
   - dx,dy = 使 LibRaw 像素与 ADC 像素对齐的偏移。做法：把 ADC DNG 的
     CFA 数据按 tile（0x0144/0x0145）拼接并用无损 JPEG 解码（或用项目里
     tools/compare_payloads.py 的取载荷函数），与 LibRaw 像素做二维互相关，
     取相关系数最大的整数偏移。窗口必须是**逐相机常量**：同一台机身的任意
     两张照片应得到同一个 (dx,dy,w,h)，若不一致说明对齐方法有错，不要提交。

c) 若样张的 ISO/模式已知，顺带记录 neutral_iso 与 noise_iso；不确定就跳过。

d) 在 ref/model_aliases.json 里加两行（LibRaw 的 make/model 与记录键都可能不同）：
   "<规范化(make+model)>": "<Camera>"
   "<规范化(model)>": "<Camera>"
   规范化 = 转小写并去掉所有非字母数字字符。例如 "DC-S5M2" → "dcs5m2"。

======================
3. 验证（缺一不可）
======================

用新样张跑转换：
   python -B tools/convert.py <样张 raw> -o <临时目录>

逐条检查，任何一条不过就修数据，不要改验证标准：
 1) 命令返回 0，输出目录出现 .dng，体积与 ADC 产出相差不超过 5%。
 2) 用 Tiff 解析该 DNG：必须能读出 4 个 IFD（ifd0 / 两个 subifd / exif），
    CFA 子 IFD 的 ImageWidth/ImageLength 与 ADC 相同，preview 子 IFD 宽在 512~2048。
 3) 预览能被 PIL 打开，尺寸 ≥512 宽。
 4) Exif 里能读到 ISO、ExposureTime、FNumber、FocalLength，且与
    tools/ingest_libraw.py 输出的元数据一致。
 5) 色彩：取 CFA 中心 512×512 区域，分别按 ADC 的 ColorMatrix 与本项目写的
    ColorMatrix 做同样的黑白电平归一 + 白平衡 + 矩阵变换，两者平均绝对差
    应小于 2/255（仅用于发现抄错的矩阵）。
 6) 灰度中性：样张若有灰卡/中性区域，变换后 RGB 三通道应接近相等
    （差异 < 6/255）；偏色说明 ColorMatrix 或 AsShotNeutral 抄错了。
 7) 把结果与 ADC 产出的 DNG 并排给我看（列出两边关键标签的值），并明确写出
    "哪些标签一致、哪些不同、不同的原因"。

======================
4. 强制规则（违反就不要提交）
======================

* 不允许编造任何数值。取不到的标签就省略，不要填 0 或占位值。
* 0xC74E（OpcodeList3，镜头畸变校正）**只有在样张所用镜头与被支持机型常见镜头
  一致时才可以写入**：这组系数是 ADC 逐镜头计算出来的，不是 raw 里的现成数据，
  换一支镜头就完全错误。写入前必须确认。
* 不允许把 ADC 的整段输出当作"模板"复制；只允许提取标签值。
* 私有数据 0xC634 只在能证明是"源 raw 中已有数据的逐字节拷贝"时才写；
  Panasonic RW2 这类 ADC 自行合成的块一律留空。
* 不要修改 tools/、src/ 里的代码，除非是修 bug；新增相机只应改 ref/ 下的 JSON。

======================
5. 交付内容
======================

1) ref/camera_db.json 中新增的相机条目（完整的 JSON 片段）。
2) ref/model_aliases.json 中新增的映射。
3) 一份简短报告，包含：
   - 样张文件名、相机名、镜头名、ISO/快门/光圈/焦距
   - ADC 版本与可执行文件路径
   - 第 3 节 7 条验证的实际输出（贴命令输出，不要只说"通过"）
   - 与 ADC 输出的标签差异清单及原因
   - 若某项无法完成（例如机器上没有该相机、没有 ADC），明确写出来并停止，
     不要用近似相机代替。

======================
6. 常见坑（先看再动手）
======================

* ADC 每次运行都会写入当前时间与随机 UUID，所以"逐字节相同"永远不可能；
  比较时必须忽略 0x0132 DateTime、0xC71B、XMP 里的 iid/did 与时间。
* ADC 不幂等（同一 raw 跑两次输出不同），所以不要用"两次 ADC 输出相同"当校验。
* 相机名要用 ADC 的写法（"Panasonic Lumix DC-S5II"），不要用机身刻印
  （"DC-S5M2"）；后者放进 model_aliases。
* BlackLevel/WhiteLevel 的类型是 RATIONAL（type 5），写成整数会被写入器当成
  另一种类型，导致标签错。
* 输出路径不要包含空格：dngbuild.exe 按空格切分参数。工作目录默认落在
  %TEMP%\jtdc，这是刻意设计。
* 若 raw 是 X-Trans（Fuji）或三色线性（Sigma/Foveon、Hasselblad 3FR），
  CFAPattern 相关字段不同，务必按 ADC 输出的实际标签抄，不要套 Bayer 模板。
````

---

## 给维护者的两点提示

* 这段提示词要求对方**必须有 ADC**。若你想放宽（例如允许用 DNG Converter 的
  试用版或其他版本），在第 1 节把版本要求改成一个范围即可，其余不用动。
* PR 里最容易被忽略的是 `window`：它必须是逐相机常量。审 PR 时让对方给出
  "同机身两张不同照片得到相同窗口"的证据，否则合并后会出现半像素错位。
