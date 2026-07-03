# visualize_swipes.py
import random
import math
import matplotlib.pyplot as plt
import matplotlib.patches as patches

# Phone screen size (matches your S23: 1080x2340)
SCREEN_W = 1080
SCREEN_H = 2340

# Same hand behavior as the real script
HAND_SWITCH_CHANCE = 0.04
LEFT_HAND_STRETCH = (3, 8)


class HandState:
    def __init__(self, dominant="right"):
        self.dominant = dominant
        self.current = dominant
        self.swipes_remaining_in_switch = 0
    
    def step(self):
        if self.swipes_remaining_in_switch > 0:
            self.swipes_remaining_in_switch -= 1
            if self.swipes_remaining_in_switch == 0:
                self.current = self.dominant
        else:
            if random.random() < HAND_SWITCH_CHANCE:
                non_dominant = "left" if self.dominant == "right" else "right"
                self.current = non_dominant
                self.swipes_remaining_in_switch = random.randint(*LEFT_HAND_STRETCH)
        return self.current


def ease_in_out(t):
    return t * t * (3 - 2 * t)


def generate_curved_swipe_points(x_start, y_start, x_end, y_end, hand, num_points=20):
    """Same logic as the real script but with more points so the curve looks smooth in the plot."""
    if hand == "right":
        arc_strength = -random.uniform(20, 50)
    else:
        arc_strength = random.uniform(20, 50)
    
    dx = x_end - x_start
    dy = y_end - y_start
    
    points = []
    for i in range(num_points):
        t_linear = i / (num_points - 1)
        t = ease_in_out(t_linear)
        
        x = x_start + dx * t
        y = y_start + dy * t
        
        arc_offset = math.sin(t_linear * math.pi) * arc_strength
        x += arc_offset
        
        x += random.uniform(-2, 2)
        y += random.uniform(-2, 2)
        
        points.append((x, y))
    
    return points


def generate_swipe_for_hand(hand):
    """Same start/end logic as human_swipe_up in main.py."""
    w, h = SCREEN_W, SCREEN_H
    
    if hand == "right":
        x_bias_center = w * 0.58
    else:
        x_bias_center = w * 0.42
    
    x_start = x_bias_center + random.uniform(-40, 40)
    y_start = h * random.uniform(0.78, 0.92)
    x_end = w * 0.5 + random.uniform(-50, 50)
    y_end = h * random.uniform(0.05, 0.18)
    
    return generate_curved_swipe_points(x_start, y_start, x_end, y_end, hand)


def main(num_swipes=30):
    hand_state = HandState(dominant="right")
    
    fig, ax = plt.subplots(figsize=(5, 10))
    
    # Draw the phone screen outline
    phone = patches.Rectangle(
        (0, 0), SCREEN_W, SCREEN_H,
        linewidth=2, edgecolor="black", facecolor="#f0f0f0"
    )
    ax.add_patch(phone)
    
    # Draw each swipe
    right_count = 0
    left_count = 0
    for i in range(num_swipes):
        hand = hand_state.step()
        points = generate_swipe_for_hand(hand)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        
        if hand == "right":
            color = "tab:blue"
            right_count += 1
        else:
            color = "tab:red"
            left_count += 1
        
        ax.plot(xs, ys, color=color, alpha=0.45, linewidth=1.3)
        # Mark start (filled circle) and end (X)
        ax.scatter(xs[0], ys[0], color=color, s=22, alpha=0.7, zorder=3)
        ax.scatter(xs[-1], ys[-1], color=color, marker="x", s=30, alpha=0.7, zorder=3)
    
    # Phone is portrait with origin at top-left in real coords; flip Y so it looks right
    ax.set_xlim(-50, SCREEN_W + 50)
    ax.set_ylim(SCREEN_H + 50, -50)  # inverted: 0 at top, max at bottom
    ax.set_aspect("equal")
    ax.set_xlabel("X (px)")
    ax.set_ylabel("Y (px)")
    ax.set_title(
        f"{num_swipes} simulated swipes\n"
        f"blue = right hand ({right_count}), red = left hand ({left_count})\n"
        f"● = start, ✕ = end"
    )
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig("swipe_pattern.png", dpi=120)
    print("Saved to swipe_pattern.png")
    plt.show()


if __name__ == "__main__":
    main(num_swipes=30)