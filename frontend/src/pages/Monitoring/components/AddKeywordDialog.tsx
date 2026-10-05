/**
 * Dialog for adding extra monitoring keywords.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

interface AddKeywordDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    newKeyword: { keyword: string; target_brand: string };
    onNewKeywordChange: (val: { keyword: string; target_brand: string }) => void;
    onAdd: () => void;
}

export function AddKeywordDialog({ open, onOpenChange, newKeyword, onNewKeywordChange, onAdd }: AddKeywordDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-[95vw] sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle>添加额外词条</DialogTitle>
                </DialogHeader>
                <div className="space-y-4">
                    <div>
                        <Label>关键词</Label>
                        <Input
                            placeholder="如：深圳GEO优化公司推荐"
                            value={newKeyword.keyword}
                            onChange={e => onNewKeywordChange({ ...newKeyword, keyword: e.target.value })}
                        />
                    </div>
                    <div>
                        <Label>目标品牌</Label>
                        <Input
                            placeholder="如：你的品牌名"
                            value={newKeyword.target_brand}
                            onChange={e => onNewKeywordChange({ ...newKeyword, target_brand: e.target.value })}
                        />
                    </div>
                    <div className="flex justify-end gap-2">
                        <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
                        <Button onClick={onAdd}>添加</Button>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
