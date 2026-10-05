import { authFetch } from '@/lib/api';
import { useState, useEffect, createContext, useContext } from 'react';

/**
 * 项目上下文 - 全局项目选择状态管理
 * P1新增：实现三要素自动注入的前端支撑
 */

export interface Project {
    id: number;
    name: string;
    industry: string;
    business?: string;
    target_audience?: string;
    product_intro?: string;
    status: string;
    created_at: string;
    // 人设信息
    persona?: {
        one_liner?: string;
        speaking_style?: string;
        catchphrase?: string;
    };
    // 绑定的顾问ID
    advisor_id?: string;
}

interface ProjectContextType {
    projects: Project[];
    currentProject: Project | null;
    isLoading: boolean;
    setCurrentProject: (project: Project | null) => void;
    fetchProjects: () => Promise<void>;
    // 获取三要素上下文（用于API调用）
    getContextData: () => {
        company_data: Record<string, string> | null;
        persona_data: Record<string, string> | null;
        advisor_id: string;
    };
}

const ProjectContext = createContext<ProjectContextType | null>(null);

export const ProjectProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [projects, setProjects] = useState<Project[]>([]);
    const [currentProject, setCurrentProject] = useState<Project | null>(null);
    const [isLoading, setIsLoading] = useState(true);

    // 从localStorage恢复上次选择的项目
    useEffect(() => {
        const savedProjectId = localStorage.getItem('currentProjectId');
        if (savedProjectId) {
            fetchProjects().then(() => {
                // 在projects加载后恢复选中状态
            });
        } else {
            fetchProjects();
        }
    }, []);

    // 当projects加载完成后，恢复上次选择的项目
    useEffect(() => {
        const savedProjectId = localStorage.getItem('currentProjectId');
        if (savedProjectId && projects.length > 0 && !currentProject) {
            const found = projects.find(p => p.id === parseInt(savedProjectId));
            if (found) {
                setCurrentProject(found);
            }
        }
    }, [projects]);

    // 保存当前选择到localStorage
    useEffect(() => {
        if (currentProject) {
            localStorage.setItem('currentProjectId', String(currentProject.id));
        } else {
            localStorage.removeItem('currentProjectId');
        }
    }, [currentProject]);

    const fetchProjects = async () => {
        try {
            setIsLoading(true);
            const response = await authFetch('/api/social/projects');
            const data = await response.json();
            if (data.status === 'success') {
                setProjects(data.projects);
            }
        } catch (error) {
            console.error('获取项目列表失败:', error);
        } finally {
            setIsLoading(false);
        }
    };

    // 获取三要素上下文数据（供API调用时使用）
    const getContextData = () => {
        if (!currentProject) {
            return {
                company_data: null,
                persona_data: null,
                advisor_id: 'xuehui', // 默认顾问
            };
        }

        return {
            company_data: {
                industry: currentProject.industry || '',
                business: currentProject.business || '',
                target_audience: currentProject.target_audience || '',
                product_intro: currentProject.product_intro || '',
            },
            persona_data: currentProject.persona ? {
                one_liner: currentProject.persona.one_liner || '',
                speaking_style: currentProject.persona.speaking_style || '',
                catchphrase: currentProject.persona.catchphrase || '',
            } : null,
            advisor_id: currentProject.advisor_id || 'xuehui',
        };
    };

    return (
        <ProjectContext.Provider value={{
            projects,
            currentProject,
            isLoading,
            setCurrentProject,
            fetchProjects,
            getContextData,
        }}>
            {children}
        </ProjectContext.Provider>
    );
};

// Hook: 使用项目上下文
export const useProject = () => {
    const context = useContext(ProjectContext);
    if (!context) {
        throw new Error('useProject must be used within ProjectProvider');
    }
    return context;
};

export default useProject;
