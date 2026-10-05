import { authFetch } from '@/lib/api';
import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { 
    Loader2, 
    MessageSquare, 
    ArrowLeft, 
    Search, 
    Building2, 
    Home, 
    Clock, 
    Users,
    Calendar,
    Trash2,
    Eye,
    BarChart3,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "@/components/SafeMarkdown";
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
// 会议记录类型
interface MeetingRecord {
    id: number;
    meeting_id: string;
    topic: string;
    brand_id?: number;
    brand_name?: string;
    is_internal: boolean;
    participants: string[];
    moderator_name?: string;
    conclusion?: string;
    summary?: string;
    status: string;
    rounds: number;
    duration?: number;
    tags?: string[];
    created_at: string;
    completed_at?: string;
}

// 会议详情类型
interface MeetingDetail extends MeetingRecord {
    context?: string;
    transcript?: any[];
    action_items?: any[];
    attachments?: any[];
}

// 统计信息类型
interface MeetingStats {
    total: number;
    this_week: number;
    internal_count: number;
    client_count: number;
    top_brands: { name: string; count: number }[];
}

export function MeetingHistory() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const navigate = useNavigate();
    
    const [meetings, setMeetings] = useState<MeetingRecord[]>([]);
    const [stats, setStats] = useState<MeetingStats | null>(null);
    const [loading, setLoading] = useState(true);
    const [total, setTotal] = useState(0);
    
    // 筛选条件
    const [search, setSearch] = useState("");
    const [filterType, setFilterType] = useState<string>("all"); // all, internal, client
    
    // 详情弹窗
    const [selectedMeeting, setSelectedMeeting] = useState<MeetingDetail | null>(null);
    const [detailLoading, setDetailLoading] = useState(false);
    const [showDetail, setShowDetail] = useState(false);

    useEffect(() => {
        fetchMeetings();
        fetchStats();
    }, [filterType]);

    const fetchMeetings = async () => {
        setLoading(true);
        try {
            let url = `/api/meeting-records?limit=100`;
            if (filterType === "internal") {
                url += "&is_internal=true";
            } else if (filterType === "client") {
                url += "&is_internal=false";
            }
            if (search) {
                url += `&search=${encodeURIComponent(search)}`;
            }
            
            const response = await authFetch(url);
            const data = await response.json();
            if (data.success) {
                setMeetings(data.meetings || []);
                setTotal(data.total || 0);
            }
        } catch (error) {
            console.error("Failed to fetch meetings:", error);
        } finally {
            setLoading(false);
        }
    };

    const fetchStats = async () => {
        try {
            const response = await authFetch("/api/meeting-records/stats");
            const data = await response.json();
            if (data.success) {
                setStats(data.stats);
            }
        } catch (error) {
            console.error("Failed to fetch stats:", error);
        }
    };

    const openDetail = async (meetingId: string) => {
        setDetailLoading(true);
        setShowDetail(true);
        try {
            const response = await authFetch(`/api/meeting-records/${meetingId}`);
            const data = await response.json();
            if (data.success) {
                setSelectedMeeting(data.meeting);
            }
        } catch (error) {
            console.error("Failed to fetch meeting detail:", error);
        } finally {
            setDetailLoading(false);
        }
    };

    const deleteMeeting = async (meetingId: string) => {
        if (!(await askConfirm({ title: "确定要删除这条会议记录吗？", danger: true }))) return;
        
        try {
            const response = await authFetch(`/api/meeting-records/${meetingId}`, {
                method: "DELETE",
            });
            const data = await response.json();
            if (data.success) {
                setMeetings(meetings.filter(m => m.meeting_id !== meetingId));
                fetchStats();
            }
        } catch (error) {
            console.error("Failed to delete meeting:", error);
        }
    };

    const handleSearch = () => {
        fetchMeetings();
    };

    const formatDate = (dateStr: string) => {
        const date = new Date(dateStr);
        return date.toLocaleDateString("zh-CN", {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    };

    const formatDuration = (seconds?: number) => {
        if (!seconds) return "-";
        if (seconds < 60) return `${Math.round(seconds)}秒`;
        return `${Math.round(seconds / 60)}分钟`;
    };

    return (
        <div className="p-6 space-y-6 min-h-[calc(100dvh-3rem)] bg-linear-to-br from-slate-50 to-blue-50">
            {/* 页面标题 */}
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                    <Button variant="ghost" size="sm" onClick={() => navigate("/employees")}>
                        <ArrowLeft className="h-4 w-4 mr-1" />
                        返回员工大厅
                    </Button>
                    <div>
                        <h1 className="text-2xl font-bold flex items-center gap-2">
                            <MessageSquare className="h-6 w-6 text-blue-600" />
                            会议记录库
                        </h1>
                        <p className="text-gray-500 mt-1">
                            查看和管理所有AI圆桌会议记录
                        </p>
                    </div>
                </div>
                <Button onClick={() => navigate("/employees/meeting")}>
                    <MessageSquare className="h-4 w-4 mr-2" />
                    召开新会议
                </Button>
            </div>

            {/* 统计卡片 */}
            {stats && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    <Card>
                        <CardContent className="pt-4">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-blue-100 rounded-lg">
                                    <BarChart3 className="h-5 w-5 text-blue-600" />
                                </div>
                                <div>
                                    <p className="text-2xl font-bold">{stats.total}</p>
                                    <p className="text-sm text-gray-500">总会议数</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                    <Card>
                        <CardContent className="pt-4">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-green-100 rounded-lg">
                                    <Calendar className="h-5 w-5 text-green-600" />
                                </div>
                                <div>
                                    <p className="text-2xl font-bold">{stats.this_week}</p>
                                    <p className="text-sm text-gray-500">本周会议</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                    <Card>
                        <CardContent className="pt-4">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-purple-100 rounded-lg">
                                    <Home className="h-5 w-5 text-purple-600" />
                                </div>
                                <div>
                                    <p className="text-2xl font-bold">{stats.internal_count}</p>
                                    <p className="text-sm text-gray-500">内部会议</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                    <Card>
                        <CardContent className="pt-4">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-orange-100 rounded-lg">
                                    <Building2 className="h-5 w-5 text-orange-600" />
                                </div>
                                <div>
                                    <p className="text-2xl font-bold">{stats.client_count}</p>
                                    <p className="text-sm text-gray-500">客户会议</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 筛选栏 */}
            <Card>
                <CardContent className="pt-4">
                    <div className="flex flex-wrap gap-4 items-center">
                        <div className="flex-1 min-w-[200px]">
                            <div className="relative">
                                <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
                                <Input
                                    placeholder="搜索会议主题、结论..."
                                    value={search}
                                    onChange={(e) => setSearch(e.target.value)}
                                    onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                                    className="pl-10"
                                />
                            </div>
                        </div>
                        <Select value={filterType} onValueChange={setFilterType}>
                            <SelectTrigger className="w-[140px]">
                                <SelectValue placeholder="全部会议" />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value="all">全部会议</SelectItem>
                                <SelectItem value="internal">内部会议</SelectItem>
                                <SelectItem value="client">客户会议</SelectItem>
                            </SelectContent>
                        </Select>
                        <Button onClick={handleSearch}>
                            <Search className="h-4 w-4 mr-2" />
                            搜索
                        </Button>
                    </div>
                </CardContent>
            </Card>

            {/* 会议列表 */}
            <Card>
                <CardHeader>
                    <CardTitle className="text-base flex items-center justify-between">
                        <span>会议记录 ({total})</span>
                    </CardTitle>
                </CardHeader>
                <CardContent>
                    {loading ? (
                        <div className="flex items-center justify-center py-12">
                            <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
                        </div>
                    ) : meetings.length === 0 ? (
                        <div className="text-center py-12 text-gray-400">
                            <MessageSquare className="h-12 w-12 mx-auto mb-4 opacity-30" />
                            <p>暂无会议记录</p>
                            <Button
                                variant="link"
                                onClick={() => navigate("/employees/meeting")}
                                className="mt-2"
                            >
                                召开第一次会议
                            </Button>
                        </div>
                    ) : (
                        <div className="space-y-3">
                            {meetings.map((meeting) => (
                                <div
                                    key={meeting.meeting_id}
                                    className="border rounded-lg p-4 hover:bg-gray-50 transition-colors"
                                >
                                    <div className="flex items-start justify-between gap-4">
                                        <div className="flex-1 min-w-0">
                                            <div className="flex items-center gap-2 mb-1">
                                                <h3 className="font-semibold text-gray-900 truncate">
                                                    {meeting.topic}
                                                </h3>
                                                {meeting.is_internal ? (
                                                    <Badge variant="outline" className="bg-purple-50 text-purple-700">
                                                        <Home className="h-3 w-3 mr-1" />
                                                        内部
                                                    </Badge>
                                                ) : meeting.brand_name && (
                                                    <Badge variant="outline" className="bg-blue-50 text-blue-700">
                                                        <Building2 className="h-3 w-3 mr-1" />
                                                        {meeting.brand_name}
                                                    </Badge>
                                                )}
                                            </div>
                                            
                                            <div className="flex flex-wrap items-center gap-4 text-sm text-gray-500 mb-2">
                                                <span className="flex items-center gap-1">
                                                    <Calendar className="h-3.5 w-3.5" />
                                                    {formatDate(meeting.created_at)}
                                                </span>
                                                <span className="flex items-center gap-1">
                                                    <Clock className="h-3.5 w-3.5" />
                                                    {formatDuration(meeting.duration)}
                                                </span>
                                                <span className="flex items-center gap-1">
                                                    <Users className="h-3.5 w-3.5" />
                                                    {meeting.participants?.length || 0}人
                                                </span>
                                                <span>{meeting.rounds}轮讨论</span>
                                            </div>
                                            
                                            {meeting.conclusion && (
                                                <p className="text-sm text-gray-600 line-clamp-2">
                                                    {meeting.conclusion.slice(0, 150)}...
                                                </p>
                                            )}
                                        </div>
                                        
                                        <div className="flex items-center gap-2">
                                            <Button
                                                variant="outline"
                                                size="sm"
                                                onClick={() => openDetail(meeting.meeting_id)}
                                            >
                                                <Eye className="h-4 w-4 mr-1" />
                                                查看
                                            </Button>
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                className="text-red-500 hover:text-red-700 hover:bg-red-50"
                                                onClick={() => deleteMeeting(meeting.meeting_id)}
                                            >
                                                <Trash2 className="h-4 w-4" />
                                            </Button>
                                        </div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </CardContent>
            </Card>

            {/* 会议详情弹窗 */}
            <Dialog open={showDetail} onOpenChange={setShowDetail}>
                <DialogContent className="max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2">
                            <MessageSquare className="h-5 w-5 text-blue-600" />
                            会议详情
                        </DialogTitle>
                    </DialogHeader>
                    
                    {detailLoading ? (
                        <div className="flex items-center justify-center py-12">
                            <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
                        </div>
                    ) : selectedMeeting && (
                        <div className="overflow-y-auto flex-1 space-y-4 pr-2">
                            {/* 基本信息 */}
                            <div className="bg-linear-to-r from-blue-50 to-indigo-50 p-4 rounded-lg">
                                <h3 className="font-semibold text-lg mb-2">{selectedMeeting.topic}</h3>
                                <div className="flex flex-wrap gap-4 text-sm text-gray-600">
                                    <span className="flex items-center gap-1">
                                        <Calendar className="h-4 w-4" />
                                        {formatDate(selectedMeeting.created_at)}
                                    </span>
                                    <span className="flex items-center gap-1">
                                        <Clock className="h-4 w-4" />
                                        {formatDuration(selectedMeeting.duration)}
                                    </span>
                                    {selectedMeeting.is_internal ? (
                                        <Badge className="bg-purple-100 text-purple-700">
                                            <Home className="h-3 w-3 mr-1" />
                                            内部会议
                                        </Badge>
                                    ) : selectedMeeting.brand_name && (
                                        <Badge className="bg-blue-100 text-blue-700">
                                            <Building2 className="h-3 w-3 mr-1" />
                                            {selectedMeeting.brand_name}
                                        </Badge>
                                    )}
                                </div>
                                <div className="mt-2 text-sm text-gray-600">
                                    <Users className="h-4 w-4 inline mr-1" />
                                    参会：{selectedMeeting.participants?.join("、")}
                                </div>
                            </div>

                            {/* 会议结论 */}
                            {selectedMeeting.conclusion && (
                                <div className="border rounded-lg p-4">
                                    <h4 className="font-medium mb-2 flex items-center gap-2">
                                        <Badge className="bg-amber-100 text-amber-700">会议结论</Badge>
                                    </h4>
                                    <div className="prose prose-sm dark:prose-invert max-w-none">
                                        <ReactMarkdown>
                                            {selectedMeeting.conclusion}
                                        </ReactMarkdown>
                                    </div>
                                </div>
                            )}

                            {/* 讨论记录 */}
                            {selectedMeeting.transcript && selectedMeeting.transcript.length > 0 && (
                                <div className="border rounded-lg p-4">
                                    <h4 className="font-medium mb-3">讨论记录</h4>
                                    <div className="space-y-3 max-h-[400px] overflow-y-auto">
                                        {selectedMeeting.transcript.map((item: any, index: number) => (
                                            <div key={index} className="flex gap-3">
                                                <div className="w-8 h-8 rounded-full bg-blue-500 shrink-0 flex items-center justify-center text-white text-xs font-bold">
                                                    {item.speaker?.slice(0, 2)}
                                                </div>
                                                <div className="flex-1">
                                                    <div className="flex items-center gap-2 mb-1">
                                                        <span className="font-medium text-sm">{item.speaker}</span>
                                                        {item.type === "opening" && (
                                                            <Badge variant="outline" className="text-xs">开场</Badge>
                                                        )}
                                                        {item.type === "conclusion" && (
                                                            <Badge variant="outline" className="text-xs">总结</Badge>
                                                        )}
                                                        {item.type === "discussion" && (
                                                            <Badge variant="outline" className="text-xs">第{item.round}轮</Badge>
                                                        )}
                                                    </div>
                                                    <div className="text-sm text-gray-700 bg-gray-50 rounded-lg p-3">
                                                        <ReactMarkdown>
                                                            {item.content}
                                                        </ReactMarkdown>
                                                    </div>
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </DialogContent>
            </Dialog>
          {confirmDialog}
        </div>
    );
}
