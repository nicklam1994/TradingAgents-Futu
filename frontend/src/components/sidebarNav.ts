import type { LucideIcon } from 'lucide-react'
import {
    BarChart3,
    BookOpen,
    Bot,
    Briefcase,
    Cpu,
    FileText,
    MessageSquare,
    Settings,
    Wallet,
} from 'lucide-react'

export interface SidebarNavItem {
    path: string
    icon: LucideIcon
    label: string
}

export const navItems: SidebarNavItem[] = [
    // { path: '/', icon: LayoutDashboard, label: '控制台' },
    // { path: '/watchlist', icon: Star, label: '优质自选' },
    // { path: '/analysis', icon: Activity, label: '智能分析' },
    { path: '/reports', icon: FileText, label: '历史报告' },
    { path: '/portfolio', icon: Briefcase, label: '定时分析' },
    { path: '/tracking-board', icon: Wallet, label: '跟踪看板' },
    { path: '/sim-trading', icon: BarChart3, label: '模拟交易' },
    { path: '/autonomous', icon: Bot, label: '自主交易' },
    { path: '/performance', icon: BarChart3, label: '绩效分析' },
    { path: '/strategies', icon: Cpu, label: '策略管理' },
    { path: '/reflections', icon: BookOpen, label: '交易反思' },
    { path: '/feedback', icon: MessageSquare, label: '反馈留言' },
    { path: '/settings', icon: Settings, label: '设置' },
]
