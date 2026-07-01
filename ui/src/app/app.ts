import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';

interface PostType {
  id: 'post' | 'carousel' | 'reel' | 'story';
  label: string;
  icon: string;
  enabled: boolean;
}

@Component({
  selector: 'app-root',
  imports: [CommonModule, FormsModule],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  readonly types: PostType[] = [
    { id: 'post', label: 'Post', icon: '📷', enabled: true },
    { id: 'carousel', label: 'Carousel', icon: '🖼️', enabled: false },
    { id: 'reel', label: 'Reel', icon: '🎬', enabled: true },
    { id: 'story', label: 'Story', icon: '⭕', enabled: false },
  ];

  view: 'picker' | 'form' = 'picker';
  currentType: 'post' | 'reel' = 'post';
  selectedFile: File | null = null;
  previewUrl: string | null = null;
  reelUrl = '';
  caption = '';
  hashtags = '';
  submitting = false;
  submitMode: 'queue' | 'publish' | null = null;
  okId: string | null = null;
  okStatus: 'queued' | 'queued_and_posted' | 'queued_but_post_failed' | null = null;
  errMsg: string | null = null;

  constructor(private http: HttpClient) {}

  get isReel(): boolean {
    return this.currentType === 'reel';
  }

  get acceptTypes(): string {
    return this.isReel ? 'video/mp4,video/quicktime' : 'image/jpeg,image/png';
  }

  pick(type: PostType): void {
    if (!type.enabled) return;
    if (type.id === 'post' || type.id === 'reel') {
      this.currentType = type.id;
      this.view = 'form';
      this.clearResult();
    }
  }

  back(): void {
    this.view = 'picker';
    this.resetForm();
    this.clearResult();
  }

  onFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0] ?? null;
    this.selectedFile = file;
    if (this.previewUrl) {
      URL.revokeObjectURL(this.previewUrl);
      this.previewUrl = null;
    }
    if (file) {
      this.previewUrl = URL.createObjectURL(file);
    }
  }

  canSubmit(): boolean {
    const hasUrl = this.reelUrl.trim().length > 0;
    return (hasUrl || !!this.selectedFile) && !this.submitting;
  }

  submit(publish: boolean = false): void {
    if (!this.canSubmit()) return;

    this.submitting = true;
    this.submitMode = publish ? 'publish' : 'queue';
    this.clearResult();

    const formData = new FormData();
    if (this.selectedFile) {
      formData.append('image', this.selectedFile);
    }
    if (this.reelUrl.trim()) {
      formData.append('url', this.reelUrl.trim());
    }
    formData.append('caption', this.caption);
    formData.append('hashtags', this.hashtags);
    formData.append('post_type', this.currentType);
    formData.append('publish', publish ? 'true' : 'false');

    this.http
      .post<{ id: string; status: string; post_error?: string }>('/api/posts', formData)
      .subscribe({
        next: (response) => {
          this.submitting = false;
          this.submitMode = null;
          this.okId = response.id;
          this.okStatus = response.status as
            | 'queued'
            | 'queued_and_posted'
            | 'queued_but_post_failed';
          if (response.post_error) {
            this.errMsg = response.post_error;
          }
          this.resetForm();
        },
        error: (err) => {
          this.submitting = false;
          this.submitMode = null;
          const msg = err?.error?.detail ?? err?.message ?? 'Unknown error';
          this.errMsg = String(msg);
        },
      });
  }

  private clearResult(): void {
    this.okId = null;
    this.okStatus = null;
    this.errMsg = null;
  }

  private resetForm(): void {
    this.selectedFile = null;
    if (this.previewUrl) {
      URL.revokeObjectURL(this.previewUrl);
      this.previewUrl = null;
    }
    this.reelUrl = '';
    this.caption = '';
    this.hashtags = '';
  }
}
