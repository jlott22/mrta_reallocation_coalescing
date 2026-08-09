from known_visit_sim.algorithms.DGA import DGAAllocator as BaseDGAAllocator

class DGAIter5Allocator(BaseDGAAllocator):
    name = "DGA_iter_5"
    DGA_ITERATIONS_PER_TRIGGER = 5
