# 07b-MCP服务管理

# 07b · MCP服务管理

> 管理员在这里管理所有 MCP Server 的注册和状态。MCP Server 是 Agent 调用业务数据的唯一通道，这个页面相当于"数据管道的总控台"。

## 页面提示词

请用 Vue 3 + Vite + Element Plus 生成一个 MCP 服务管理页面，具体要求如下：

**页面目标**：管理员查看和管理所有 MCP Server 的注册状态、启用的工具列表、心跳状态。能手动注册新服务、启用/禁用特定工具、手动触发心跳检测。

**页面布局**：左侧是 MCP 服务卡片列表（每个服务一张卡片，显示名称、地址、状态灯、工具数量），右侧是选中服务下的工具列表表格。页面顶部有搜索框和手动注册按钮。

**核心交互**：

1.  服务卡片：每张卡片展示服务名称（订单数据服务/售后工单服务/商品品类服务/物流轨迹服务/支付退款服务）、主机地址、状态圆点（绿色在线/红色离线/黄色降级）、注册的工具数量、最后心跳时间。点击卡片选中，右侧更新工具列表
    
2.  工具列表表格：列包括工具名称、描述、参数 Schema（可折叠展开）、启用开关。点击开关即时切换工具启用状态
    
3.  手动注册服务：点击"注册服务"按钮弹出 Dialog，填写服务名称、主机地址、端口
    
4.  心跳检测：每个卡片右下角有"检测"按钮，点击后手动触发一次心跳请求，更新状态
    
5.  离线服务显示灰色蒙层效果（opacity降低），提醒管理员关注
    

**数据来源**：

*   GET /api/admin/mcp/servers 获取所有MCP服务及心跳状态
    
*   GET /api/admin/mcp/servers/:id/tools 获取服务下的工具列表
    
*   POST /api/admin/mcp/servers 注册新服务
    
*   PUT /api/admin/mcp/servers/:id 编辑服务信息
    
*   PUT /api/admin/mcp/tools/:id/toggle 切换工具启用状态
    
*   POST /api/admin/mcp/servers/:id/heartbeat 手动触发心跳
    

**UI 组件清单**：

*   el-card（MCP 服务卡片，含状态指示）
    
*   el-tag（状态标签：success=在线/danger=离线/warning=降级）
    
*   el-table（工具列表，列：工具名、描述、参数概要、启用开关）
    
*   el-dialog（注册/编辑服务表单）
    
*   el-form + el-form-item
    
*   el-input（服务名称、主机地址、端口）
    
*   el-switch（工具启用开关）
    
*   el-collapse / el-popover（参数 Schema 展开查看）
    
*   el-button（检测心跳、注册服务）
    
*   el-empty（无服务时）
    

**边界情况**：

*   无已注册服务时，显示 Empty："暂无注册的 MCP 服务，点击上方按钮注册"
    
*   心跳超时30秒未响应，自动将状态切换为疑离线（黄色 warning + "最后心跳: 30秒前"）
    
*   某个服务的工具列表为空时，右侧表格显示"该服务暂未注册工具"
    
*   注册服务时主机地址格式校验（host:port 正则）
    
*   切换工具开关时即时 loading 效果（el-switch 的 loading 属性）