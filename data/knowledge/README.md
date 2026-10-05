# 统一知识库目录

## 目录结构

```
knowledge/
├── public/                 # 公共知识库（所有角色共享）
│   ├── geo_methodology/    # GEO方法论
│   └── industry_insights/  # 行业知识
├── clients/                # 客户知识库（按brand_id隔离）
│   └── {brand_id}/         # 如：38、39...
└── roles/                  # 角色私有库
    ├── advisors/           # 顾问私有库（如：huang-douyin）
    └── employees/          # 员工私有库（如：marketing_director）
```

## 使用说明

### 公共知识库
将通用方法论、行业知识放入 `public/` 目录，所有角色都可检索。

### 客户知识库
按客户brand_id创建子目录，存放该客户专属的业务资料。

### 角色私有库
按角色类型和ID创建子目录，存放角色独有的专业知识。

## 支持格式
- `.md` Markdown文件
- `.txt` 纯文本
- `.pdf` PDF文档（需额外处理）
