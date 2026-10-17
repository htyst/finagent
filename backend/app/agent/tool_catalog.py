"""Discoverable capabilities and explicit banking-simulator boundaries."""
from app.domain.errors import DomainError

READ_DESCRIPTIONS={
 'read_service_requests':'读取本人补卡、信用卡调额申请和会员续订取消的固定单与审批回执。kind可选replace_card/credit_limit/cancel_contract。指定会员记录需订阅查询确认；默认未获授权时不返回会员记录。只查状态，不代替本人确认或独立审批。',
 'read_credit_cards':'查询本人导入信用卡的总额度、档案欠款、可用额度、已出账/未出账和最近账单及还款日。card可填本人卡尾号、名称或id，不填查询全部本人信用卡；缺失返回未知，标明快照日期，不代表实时可刷额度，不办理调额或还款。',
 'read_tool_catalog':'查询本系统实际可调用的工具、六类赛题场景、权限等级和业务边界；能力问题优先使用，不猜支持范围。',
 'read_wealth_products':'查询模拟理财产品的风险、起购金额、费用、锁定期及收益说明；可指定product_id，不代表推荐或申购。',
 'read_risk_assessment':'只读本人当前风险评估、有效性和问卷规则；没有有效评估要如实说明，模型不能代答问卷。',
 'read_investment_performance':'查询本人持仓市值与导入档案中的成本、浮动盈亏和估值日期。快照收益不是实时收益，缺少原生交易成本核算时返回未知，不按产品展示收益率编造实际收益。',
 'read_birthday_offers':'查询可信的本地模拟鲜花和蛋糕供应商及固定报价，供生日安排使用；不预留资金、不下单、不获取私人关系人资料。',
}


def catalog():
    from app.agent import dynamic_protocol as p
    from app.agent.dialogue import READ_TOOLS
    names=set(p.models());groups={
      '智能转账':['list_accounts','list_recipients','prepare_transfer','prepare_schedule','prepare_scheduled_transfer','prepare_collection','prepare_aa_collection','list_schedules','list_collections'],
      '账单分析':['list_transactions','summarize_spending','analyze_finances','generate_statement','inspect_anomalies','read_financial_profile','read_financial_history','financial_planning_basis'],
      '理财操作':['read_wealth','read_wealth_products','read_risk_assessment','read_investment_performance','request_risk_assessment','compare_wealth_products','prepare_wealth_buy','prepare_wealth_redeem'],
      '卡片管理':['prepare_card_replacement','prepare_credit_limit_change','read_credit_cards','list_cards','read_card_management','prepare_card_loss','prepare_card_unloss','prepare_card_freeze','prepare_card_unfreeze','prepare_virtual_card_application','prepare_card_limit_change','prepare_online_payment_block','prepare_online_payment_unblock','prepare_card_action','prepare_card_management'],
      '订阅代扣':['prepare_merchant_contract_cancel','read_subscriptions','identify_subscriptions','prepare_cancel_bank_mandate','prepare_subscription_revoke','prepare_renewal_reminder','prepare_subscription_reminder'],
      '生日联动':['read_birthdays','read_birthday_offers','prepare_birthday_create','prepare_birthday_change','prepare_birthday_cancel','prepare_birthday_cancel_item','prepare_birthday_retry'],
      '通用支持':['read_service_requests','read_tool_catalog','search_knowledge','read_agent_status','plan_budget','calculate','respond','manage_task']}
    assert set().union(*(set(v) for v in groups.values()))==names
    return {'tool_count':len(names),'groups':groups,'permissions':{
        'green':'本人普通查询和分析自动执行；不等于可以读取他人或受保护订阅',
        'yellow':'订阅查询、提醒、代扣解除、虚拟卡申请、小额转账等须本人页面确认',
        'red':'挂失、解挂、补卡、授信调额申请、解冻、恢复线上支付、提高交易限额、理财申购以及日累计超1000元转账须独立模拟短信验证及确认',
        'execution':'所有prepare只准备固定单；确认凭据和执行端点不向LLM开放'},
        'boundaries':['本系统为本地合成数据银行，交易、商户及认证均为模拟',
          '解除银行代扣不取消商户会员合同，不退款；可单独取消本地模拟商户下期续订，保留权益、不退款，不代表真实平台已取消',
          '解挂须独立强验证和固定单确认；仅解除挂失，冻结与支付限制保留；模拟借记卡补发须强验证，原卡永久失效；密码修改暂未实现',
          '单笔交易限额与信用卡授信额度分开；调额须本人强验证提交及独立模拟审批',
          '导入欠款、基金等仍为只读快照；授信可在本地审批后单独调整，不开放信用卡消费或基金交易',
          '生日安排复用当前专用流程；通用关系人、资金预留与通用DAG调度留待后续扩展']}


def read(db,session_id,name,args):
    from app.domain.operations import _session,_now
    session,_=_session(db,session_id,_now())
    if name=='read_tool_catalog':return catalog()
    if name=='read_service_requests':
        from app.domain.servicing import records
        return records(db,session_id=session_id,kind=args['kind'])
    if name=='read_credit_cards':
        from app.domain.credit_cards import read as read_credit_cards
        return read_credit_cards(db,session_id=session_id,card=args['card'])
    if name=='read_birthday_offers':
        from app.domain.birthdays import ITEMS
        return {'scope':'local_simulated_suppliers','currency':'CNY','offers':[dict(item_key=k,**v) for k,v in ITEMS.items()],
            'rules':'仅报价，生日计划固定授权后才预留和下单；用户须确认生日日期、本人付款账户和配送资料'}
    from app.domain.wealth import portfolio
    data=portfolio(db,session_id=session_id)
    if name=='read_wealth_products':
        products=[p for p in data['products'] if not args['product_id'] or p['id']==args['product_id']]
        if args['product_id'] and not products:raise DomainError('RESOURCE_NOT_FOUND','模拟产品不存在',404)
        return {'products':products,'scope':'local_simulated_products','note':'展示收益说明不是实际持仓收益或未来承诺'}
    if name=='read_risk_assessment':
        return {k:data[k] for k in ('assessment','questions','assessment_rule')}
    if name=='read_investment_performance':
        from app.domain.financial_profile import dataset_manifest
        manifest=dataset_manifest(db) or {}
        fields=('holding_id','product_name','currency','current_cost_minor','market_value_minor','unrealized_pnl_minor','valuation_date')
        imported=[{k:r.get(k) for k in fields} for r in data['imported_positions']]
        for row in imported:
            for k in ('current_cost_minor','market_value_minor','unrealized_pnl_minor'):
                if row[k] is not None:row[k]=int(row[k])
        products={p['id']:p for p in data['products']}
        native=[dict(product_id=p['product_id'],units_hundredths=p['units_hundredths'],
            market_value_minor=p['units_hundredths']*products[p['product_id']]['nav_minor']//100,
            current_cost_minor=None,unrealized_pnl_minor=None,realized_pnl_minor=None) for p in data['positions']]
        return {'imported_holdings':imported,'snapshot_as_of':manifest.get('as_of') or manifest.get('snapshot_as_of') or manifest.get('period_end'),
            'native_positions':native,'queried_at_utc':_now().isoformat(),
            'limitations':['导入成本和浮动盈亏为各条valuation_date的快照，不是实时行情；不能原生赎回',
                '原生持仓当前仅提供模拟净值市值，未建立完整成本收益核算，相关盈亏为未知']}
    raise DomainError('INVALID_TOOL','未知能力查询',422)
