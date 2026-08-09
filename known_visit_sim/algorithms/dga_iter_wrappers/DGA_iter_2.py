from known_visit_sim.algorithms.DGA import DGAAllocator as BaseDGAAllocator

class DGAIter2Allocator(BaseDGAAllocator):
    name = "DGA_iter_2"
    DGA_ITERATIONS_PER_TRIGGER = 2
