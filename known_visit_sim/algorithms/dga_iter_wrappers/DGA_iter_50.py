from known_visit_sim.algorithms.DGA import DGAAllocator as BaseDGAAllocator

class DGAIter50Allocator(BaseDGAAllocator):
    name = "DGA_iter_50"
    DGA_ITERATIONS_PER_TRIGGER = 50
